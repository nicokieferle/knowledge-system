"""Validated external drafts and one atomic, durable submission transaction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Literal
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, ValidationError

from .client_state import ClientIdentity
from .conversation_models import ProposalDraft, ProposalTriggerType
from .db import connect
from .proposal_review import InvalidTarget, validate_content
from .proposal_store import PostgresProposalStore, ProposalCreate
from .sources import validate_source_target


class IngressError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status, self.code = status, code


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    source_ref: str
    origin_kind: Literal["human_statement", "document_excerpt", "model_output"]
    statement_type: Literal[
        "fact",
        "interpretation",
        "hypothesis",
        "assumption",
        "forecast",
        "decision",
        "proposal",
        "open_question",
    ]
    original_text: str


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    summary: str
    reason: str
    proposed_content: str
    provenance: Provenance
    title: str | None = None
    target_source_path: str | None = None

    @property
    def fingerprint(self) -> str:
        canonical = json.dumps(
            self.model_dump(),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise IngressError(400, "invalid_json")
        result[key] = value
    return result


def _reject_constant(_):
    raise ValueError()


def parse_submission(body: bytes) -> Submission:
    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise IngressError(400, "invalid_json") from None
    try:
        submission = Submission.model_validate(value)
    except ValidationError:
        raise IngressError(422, "invalid_submission") from None
    p = submission.provenance
    for value, limit in (
        (submission.summary, 4000),
        (submission.reason, 4000),
        (submission.proposed_content, 128000),
        (p.original_text, 128000),
        (submission.title, 256),
        (p.source_ref, 1024),
    ):
        if value is None:
            continue
        try:
            size = len(value.encode("utf-8"))
        except UnicodeError:
            raise IngressError(422, "invalid_submission") from None
        if size > limit:
            raise IngressError(413, "content_too_large")
        if not value.strip() or "\x00" in value:
            raise IngressError(422, "invalid_submission")
    try:
        validate_content(submission.proposed_content)
        if submission.target_source_path is not None:
            if len(submission.target_source_path) > 1024:
                raise IngressError(413, "content_too_large")
            validate_source_target("knowledge-git", submission.target_source_path)
    except (InvalidTarget, ValueError):
        raise IngressError(422, "invalid_submission") from None
    return submission


@dataclass(frozen=True)
class SubmissionResult:
    receipt: dict[str, str]
    created: bool


class ProposalIngressService:
    def __init__(self, settings, connection_factory=connect):
        self.settings, self._connect = settings, connection_factory
        self.proposals = PostgresProposalStore(settings, connection_factory)

    def submit(
        self, client_id: str, owner: ClientIdentity, key: UUID, submission: Submission
    ) -> SubmissionResult:
        binding = asdict(owner)
        external_id = f"v1:{client_id}:{key}"
        # The unique insert waits for competing transactions. A second statement
        # at READ COMMITTED sees their committed row, then locks the durable anchor.
        with self._connect(self.settings) as conn, conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            new = conn.execute(
                """INSERT INTO conversations
                (id, client_type, external_conversation_id, title, archived_at)
                VALUES (%s,'proposal-ingress',%s,%s,now())
                ON CONFLICT (client_type, external_conversation_id)
                WHERE external_conversation_id IS NOT NULL DO NOTHING RETURNING id""",
                (uuid4(), external_id, submission.title),
            ).fetchone()
            conversation_id = conn.execute(
                """SELECT id FROM conversations WHERE client_type='proposal-ingress'
                AND external_conversation_id=%s FOR UPDATE""",
                (external_id,),
            ).fetchone()[0]
            if new is None:
                row = conn.execute(
                    """SELECT metadata FROM messages WHERE conversation_id=%s
                    AND metadata->>'control'='external_submission'""",
                    (conversation_id,),
                ).fetchone()
                if row is None:
                    raise IngressError(503, "submission_unavailable")
                metadata = row[0]
                if metadata["owner"] != binding:
                    raise IngressError(409, "binding_conflict")
                if metadata["fingerprint"] != submission.fingerprint:
                    raise IngressError(409, "idempotency_conflict")
                result = SubmissionResult(metadata["receipt"], False)
            else:
                owner_key = (owner.client_type, owner.external_chat_id, owner.external_user_id)
                conn.execute(
                    """INSERT INTO client_states (client_type, external_chat_id, external_user_id)
                    VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""",
                    owner_key,
                )
                conn.execute(
                    """INSERT INTO client_conversations
                    (client_type, external_chat_id, external_user_id, conversation_id)
                    VALUES (%s,%s,%s,%s)""",
                    (*owner_key, conversation_id),
                )
                p = submission.provenance
                # Reversible JSON escaping keeps field lines and display direction fixed.
                source_ref_json = json.dumps(p.source_ref, ensure_ascii=True).replace(
                    "\x7f", r"\u007f"
                )
                provenance = (
                    "Eingereichte Herkunftsangaben (ungeprüfte Clientangaben):\n"
                    f"Maschinen-Client: {client_id}\nQuellenart: {p.origin_kind}\n"
                    f"Quellenreferenz (JSON-String): {source_ref_json}\n"
                    f"Aussagetyp: {p.statement_type}\n"
                    "Die separate Originalnachricht ist eingereichter Kontext; die Rolle user "
                    "bestätigt keine menschliche Autorschaft. model_output bezeichnet Modelltext."
                )
                origin_ids = tuple(
                    self._message(conn, conversation_id, text, {})
                    for text in (p.original_text, provenance)
                )
                trigger_id = self._message(
                    conn,
                    conversation_id,
                    "External proposal submission",
                    {
                        "control": "external_submission",
                        "fingerprint": submission.fingerprint,
                        "client_id": client_id,
                        "owner": binding,
                    },
                )
                proposal = self.proposals.insert_proposal_in_transaction(
                    conn,
                    ProposalCreate(
                        conversation_id,
                        client_id,
                        ProposalTriggerType.COMMAND,
                        trigger_id,
                        origin_ids,
                        ProposalDraft(
                            summary=submission.summary,
                            reason=submission.reason,
                            proposed_content=submission.proposed_content,
                            title=submission.title,
                            target_source_id="knowledge-git",
                            target_source_path=submission.target_source_path,
                        ),
                    ),
                )
                receipt = {
                    "proposal_id": str(proposal[0]),
                    "conversation_id": str(conversation_id),
                    "submission_state": "recorded",
                }
                conn.execute(
                    "UPDATE messages SET metadata=metadata || %s WHERE id=%s",
                    (Jsonb({"receipt": receipt}), trigger_id),
                )
                result = SubmissionResult(receipt, True)
        return result  # only after commit, including replay

    @staticmethod
    def _message(conn, conversation_id, content, metadata):
        return conn.execute(
            """INSERT INTO messages (conversation_id,role,content,metadata)
            VALUES (%s,'user',%s,%s) RETURNING id""",
            (conversation_id, content, Jsonb(metadata)),
        ).fetchone()[0]
