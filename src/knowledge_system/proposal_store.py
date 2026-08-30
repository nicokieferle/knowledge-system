from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID, uuid4

from .config import Settings
from .conversation_models import (
    Proposal,
    ProposalDraft,
    ProposalSuggestion,
    ProposalTriggerType,
    SuggestionStatus,
)
from .db import connect


class ProposalSuggestionNotFoundError(LookupError):
    pass


class ProposalSuggestionStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProposalCreate:
    conversation_id: UUID
    source_client: str
    trigger_type: ProposalTriggerType
    trigger_message_id: int
    originating_message_ids: tuple[int, ...]
    draft: ProposalDraft
    source_suggestion_id: UUID | None = None


class ProposalStore(Protocol):
    def create_proposal(self, proposal: ProposalCreate) -> Proposal: ...

    def create_suggestion(
        self,
        conversation_id: UUID,
        trigger_message_id: int,
        originating_message_ids: tuple[int, ...],
        context_summary: str | None,
    ) -> ProposalSuggestion: ...

    def get_suggestion(
        self,
        conversation_id: UUID,
        suggestion_id: UUID,
    ) -> ProposalSuggestion: ...

    def confirm_suggestion(
        self,
        conversation_id: UUID,
        suggestion_id: UUID,
        confirmation_message_id: int,
        source_client: str,
        draft: ProposalDraft,
    ) -> Proposal: ...

    def reject_suggestion(
        self,
        conversation_id: UUID,
        suggestion_id: UUID,
        resolution_message_id: int,
    ) -> ProposalSuggestion: ...


ConnectionFactory = Callable[[Settings], AbstractContextManager[Any]]


class PostgresProposalStore:
    def __init__(
        self,
        settings: Settings,
        connection_factory: ConnectionFactory = connect,
    ) -> None:
        self.settings = settings
        self._connect = connection_factory

    def create_proposal(self, proposal: ProposalCreate) -> Proposal:
        with self._connect(self.settings) as conn, conn.transaction():
            row = self._insert_proposal(conn, proposal)
        created = _proposal_from_row(row)
        print(f"[proposal] Proposal {created.id} is pending")
        return created

    def create_suggestion(
        self,
        conversation_id: UUID,
        trigger_message_id: int,
        originating_message_ids: tuple[int, ...],
        context_summary: str | None,
    ) -> ProposalSuggestion:
        with self._connect(self.settings) as conn, conn.transaction():
            row = conn.execute(
                """
                INSERT INTO proposal_suggestions (
                    id, conversation_id, trigger_message_id,
                    originating_message_ids, context_summary
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (conversation_id, trigger_message_id)
                DO UPDATE SET trigger_message_id = EXCLUDED.trigger_message_id
                RETURNING id, conversation_id, trigger_message_id,
                          originating_message_ids, context_summary, status,
                          created_at, resolved_at, resolution_message_id
                """,
                (
                    uuid4(),
                    conversation_id,
                    trigger_message_id,
                    list(originating_message_ids),
                    context_summary,
                ),
            ).fetchone()
        suggestion = _suggestion_from_row(row)
        print(f"[proposal] Suggestion {suggestion.id} status={suggestion.status.value}")
        return suggestion

    def get_suggestion(
        self,
        conversation_id: UUID,
        suggestion_id: UUID,
    ) -> ProposalSuggestion:
        with self._connect(self.settings) as conn:
            row = conn.execute(
                f"{_SUGGESTION_SELECT} WHERE conversation_id = %s AND id = %s",
                (conversation_id, suggestion_id),
            ).fetchone()
        if row is None:
            raise ProposalSuggestionNotFoundError(
                f"Unknown suggestion `{suggestion_id}` for conversation `{conversation_id}`"
            )
        return _suggestion_from_row(row)

    def confirm_suggestion(
        self,
        conversation_id: UUID,
        suggestion_id: UUID,
        confirmation_message_id: int,
        source_client: str,
        draft: ProposalDraft,
    ) -> Proposal:
        with self._connect(self.settings) as conn, conn.transaction():
            suggestion_row = conn.execute(
                f"{_SUGGESTION_SELECT} WHERE conversation_id = %s AND id = %s FOR UPDATE",
                (conversation_id, suggestion_id),
            ).fetchone()
            if suggestion_row is None:
                raise ProposalSuggestionNotFoundError(
                    f"Unknown suggestion `{suggestion_id}` for conversation `{conversation_id}`"
                )

            suggestion = _suggestion_from_row(suggestion_row)
            if suggestion.status is SuggestionStatus.REJECTED:
                raise ProposalSuggestionStateError("A rejected suggestion cannot be confirmed")
            if suggestion.status is SuggestionStatus.CONFIRMED:
                existing = self._get_proposal_for_suggestion(conn, suggestion_id)
                if existing is None:
                    raise ProposalSuggestionStateError(
                        "Confirmed suggestion has no proposal; durable state is inconsistent"
                    )
                return _proposal_from_row(existing)

            originating_ids = tuple(
                dict.fromkeys((*suggestion.originating_message_ids, suggestion.trigger_message_id))
            )
            proposal_create = ProposalCreate(
                conversation_id=conversation_id,
                source_client=source_client,
                trigger_type=ProposalTriggerType.CONFIRMED_SUGGESTION,
                trigger_message_id=confirmation_message_id,
                originating_message_ids=originating_ids,
                draft=draft,
                source_suggestion_id=suggestion_id,
            )
            proposal_row = self._insert_proposal(conn, proposal_create)
            if proposal_row[14] != suggestion_id:
                raise ProposalSuggestionStateError(
                    "Proposal idempotency key does not match the locked suggestion"
                )
            updated = conn.execute(
                """
                UPDATE proposal_suggestions
                SET status = 'confirmed', resolution_message_id = %s,
                    resolved_at = now()
                WHERE id = %s AND conversation_id = %s AND status = 'pending'
                RETURNING id
                """,
                (confirmation_message_id, suggestion_id, conversation_id),
            ).fetchone()
            if updated is None:
                raise ProposalSuggestionStateError("Suggestion was concurrently resolved")

        proposal = _proposal_from_row(proposal_row)
        print(f"[proposal] Suggestion {suggestion_id} confirmed as proposal {proposal.id}")
        return proposal

    def reject_suggestion(
        self,
        conversation_id: UUID,
        suggestion_id: UUID,
        resolution_message_id: int,
    ) -> ProposalSuggestion:
        with self._connect(self.settings) as conn, conn.transaction():
            row = conn.execute(
                f"{_SUGGESTION_SELECT} WHERE conversation_id = %s AND id = %s FOR UPDATE",
                (conversation_id, suggestion_id),
            ).fetchone()
            if row is None:
                raise ProposalSuggestionNotFoundError(
                    f"Unknown suggestion `{suggestion_id}` for conversation `{conversation_id}`"
                )
            suggestion = _suggestion_from_row(row)
            if suggestion.status is SuggestionStatus.CONFIRMED:
                raise ProposalSuggestionStateError("A confirmed suggestion cannot be rejected")
            if suggestion.status is SuggestionStatus.PENDING:
                row = conn.execute(
                    """
                    UPDATE proposal_suggestions
                    SET status = 'rejected', resolution_message_id = %s,
                        resolved_at = now()
                    WHERE id = %s AND conversation_id = %s AND status = 'pending'
                    RETURNING id, conversation_id, trigger_message_id,
                              originating_message_ids, context_summary, status,
                              created_at, resolved_at, resolution_message_id
                    """,
                    (resolution_message_id, suggestion_id, conversation_id),
                ).fetchone()
        rejected = suggestion if suggestion.status is SuggestionStatus.REJECTED else _suggestion_from_row(row)
        print(f"[proposal] Suggestion {suggestion_id} status={rejected.status.value}")
        return rejected

    def _insert_proposal(self, conn: Any, proposal: ProposalCreate) -> Sequence[Any]:
        draft = proposal.draft
        if proposal.source_suggestion_id is None:
            conflict_clause = """
                ON CONFLICT (conversation_id, trigger_message_id, trigger_type)
                DO UPDATE SET trigger_message_id = EXCLUDED.trigger_message_id
            """
        else:
            conflict_clause = """
                ON CONFLICT (source_suggestion_id)
                DO UPDATE SET source_suggestion_id = EXCLUDED.source_suggestion_id
            """
        return conn.execute(
            f"""
            INSERT INTO proposals (
                id, conversation_id, source_client, trigger_type,
                trigger_message_id, originating_message_ids,
                target_source_id, target_source_path, title, summary, reason,
                proposed_content, base_revision, source_suggestion_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            {conflict_clause}
            RETURNING id, conversation_id, status, source_client, trigger_type,
                      trigger_message_id, originating_message_ids, summary, reason,
                      proposed_content, title, target_source_id, target_source_path,
                      base_revision, source_suggestion_id, created_at
            """,
            (
                uuid4(),
                proposal.conversation_id,
                proposal.source_client,
                proposal.trigger_type.value,
                proposal.trigger_message_id,
                list(proposal.originating_message_ids),
                draft.target_source_id,
                draft.target_source_path,
                draft.title,
                draft.summary,
                draft.reason,
                draft.proposed_content,
                draft.base_revision,
                proposal.source_suggestion_id,
            ),
        ).fetchone()

    @staticmethod
    def _get_proposal_for_suggestion(conn: Any, suggestion_id: UUID) -> Sequence[Any] | None:
        return conn.execute(
            f"{_PROPOSAL_SELECT} WHERE source_suggestion_id = %s",
            (suggestion_id,),
        ).fetchone()


_SUGGESTION_SELECT = """
    SELECT id, conversation_id, trigger_message_id, originating_message_ids,
           context_summary, status, created_at, resolved_at, resolution_message_id
    FROM proposal_suggestions
"""

_PROPOSAL_SELECT = """
    SELECT id, conversation_id, status, source_client, trigger_type,
           trigger_message_id, originating_message_ids, summary, reason,
           proposed_content, title, target_source_id, target_source_path,
           base_revision, source_suggestion_id, created_at
    FROM proposals
"""


def _suggestion_from_row(row: Sequence[Any]) -> ProposalSuggestion:
    return ProposalSuggestion(
        id=row[0],
        conversation_id=row[1],
        trigger_message_id=row[2],
        originating_message_ids=tuple(row[3]),
        context_summary=row[4],
        status=SuggestionStatus(row[5]),
        created_at=row[6],
        resolved_at=row[7],
        resolution_message_id=row[8],
    )


def _proposal_from_row(row: Sequence[Any]) -> Proposal:
    return Proposal(
        id=row[0],
        conversation_id=row[1],
        status=row[2],
        source_client=row[3],
        trigger_type=ProposalTriggerType(row[4]),
        trigger_message_id=row[5],
        originating_message_ids=tuple(row[6]),
        summary=row[7],
        reason=row[8],
        proposed_content=row[9],
        title=row[10],
        target_source_id=row[11],
        target_source_path=row[12],
        base_revision=row[13],
        source_suggestion_id=row[14],
        created_at=row[15],
    )
