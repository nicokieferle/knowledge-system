"""Durable accepted-revision apply and document-scoped indexing orchestration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from functools import partial
from uuid import UUID, uuid4

from .client_state import ClientIdentity
from .config import Settings
from .db import connect
from .index_coordination import lock_document_index, lock_parent_write
from .indexer import (
    DocumentIndexCoordination,
    DocumentIndexer,
    DocumentIndexerContractError,
    index_document,
    validate_document_index_result,
    validate_document_indexer,
)
from .knowledge_apply import (
    KnowledgeConflict,
    KnowledgeIOFailure,
    SecureKnowledgeWriter,
)
from .proposal_review import (
    InconsistentReview,
    InvalidTransition,
    ProposalReviewService,
    ReviewConflict,
    ReviewMissing,
    ReviewView,
    content_hash,
)
from .review_store import PostgresReviewStore
from .sources import GitMarkdownSource, SourceDocument

LOG = logging.getLogger(__name__)


class ApplyStatus(StrEnum):
    PENDING = "pending"
    APPLIED = "applied"
    CONFLICT = "conflict"
    FAILED = "failed"


class IndexStatus(StrEnum):
    PENDING = "pending"
    INDEXED = "indexed"
    FAILED = "failed"


@dataclass(frozen=True)
class ApplyRecord:
    id: UUID
    proposal_id: UUID
    review_id: UUID
    target_source_id: str
    target_source_path: str
    expected_old_hash: str | None
    expected_absent: bool
    expected_new_hash: str
    apply_status: str
    index_status: str
    actual_hash: str | None
    apply_error_class: str | None
    index_error_class: str | None
    apply_attempts: int
    index_attempts: int
    intent_created_at: datetime
    last_apply_attempt_at: datetime | None
    applied_at: datetime | None
    last_index_attempt_at: datetime | None
    indexed_at: datetime | None
    refresh_proposal_id: UUID | None
    updated_at: datetime


@dataclass(frozen=True)
class ApplyView:
    review: ReviewView
    apply: ApplyRecord | None


APPLY_SELECT = """SELECT id, proposal_id, review_id, target_source_id,
 target_source_path, expected_old_hash, expected_absent, expected_new_hash,
 apply_status, index_status, actual_hash, apply_error_class, index_error_class,
 apply_attempts, index_attempts, intent_created_at, last_apply_attempt_at,
 applied_at, last_index_attempt_at, indexed_at, refresh_proposal_id, updated_at
 FROM proposal_applies"""


def _apply_from_row(row) -> ApplyRecord | None:
    return ApplyRecord(*row) if row else None


class PostgresApplyStore:
    def __init__(self, settings: Settings, connection_factory=connect) -> None:
        self.settings = settings
        self._connect = connection_factory
        self.reviews = PostgresReviewStore(settings, connection_factory)

    def _apply(self, conn, proposal_id: UUID, *, lock=False) -> ApplyRecord | None:
        suffix = " FOR UPDATE" if lock else ""
        return _apply_from_row(
            conn.execute(APPLY_SELECT + " WHERE proposal_id=%s" + suffix, (proposal_id,)).fetchone()
        )

    def _result(self, conn, identity, proposal_id) -> ApplyView:
        return ApplyView(
            self.reviews._view(conn, identity, proposal_id), self._apply(conn, proposal_id)
        )

    def get(self, identity: ClientIdentity, proposal_id: UUID) -> ApplyView:
        with self._connect(self.settings) as conn, conn.transaction():
            return self._result(conn, identity, proposal_id)

    def _insert_intent(self, conn, identity, view: ReviewView) -> None:
        if view.proposal.status != "accepted" or view.revision is None:
            raise InvalidTransition()
        if view.accepted_review_id != view.revision.id:
            raise InconsistentReview()
        c = view.revision.content
        conn.execute(
            """INSERT INTO proposal_applies
            (id, proposal_id, review_id, target_source_id, target_source_path,
             expected_old_hash, expected_absent, expected_new_hash,
             actor_client_type, actor_external_chat_id, actor_external_user_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (proposal_id) DO NOTHING""",
            (
                uuid4(),
                view.proposal.id,
                view.revision.id,
                c.target_source_id,
                c.target_source_path,
                c.old_hash,
                c.change_kind == "create",
                c.new_hash,
                identity.client_type,
                identity.external_chat_id,
                identity.external_user_id,
            ),
        )
        apply = self._apply(conn, view.proposal.id, lock=True)
        if apply is None or apply.review_id != view.revision.id:
            raise InconsistentReview()

    def accept_and_intent(
        self,
        identity: ClientIdentity,
        proposal_id: UUID,
        review_id: UUID,
        expected_status: str,
    ) -> ApplyView:
        """Commit the accepted decision and its apply intent in one DB transaction."""
        with self._connect(self.settings) as conn, conn.transaction():
            view = self.reviews._view(conn, identity, proposal_id)
            if view.proposal.status == "accepted":
                if view.accepted_review_id != review_id:
                    raise ReviewConflict()
            else:
                if view.proposal.status not in ("pending", "deferred"):
                    raise InvalidTransition()
                if view.proposal.status != expected_status:
                    raise ReviewConflict()
                if view.revision is None or view.revision.id != review_id:
                    raise ReviewConflict()
                conn.execute(
                    """INSERT INTO proposal_decisions
                    (id, proposal_id, review_id, previous_status, status,
                     client_type, external_chat_id, external_user_id)
                    VALUES (%s,%s,%s,%s,'accepted',%s,%s,%s)""",
                    (
                        uuid4(),
                        proposal_id,
                        review_id,
                        view.proposal.status,
                        identity.client_type,
                        identity.external_chat_id,
                        identity.external_user_id,
                    ),
                )
                conn.execute(
                    "UPDATE proposals SET status='accepted', accepted_review_id=%s WHERE id=%s",
                    (review_id, proposal_id),
                )
                view = self.reviews._view(conn, identity, proposal_id)
            self._insert_intent(conn, identity, view)
            return self._result(conn, identity, proposal_id)

    def ensure_intent(
        self, identity: ClientIdentity, proposal_id: UUID, review_id: UUID
    ) -> ApplyView:
        """Explicitly create an intent for an accepted pre-V3.3 proposal."""
        with self._connect(self.settings) as conn, conn.transaction():
            view = self.reviews._view(conn, identity, proposal_id)
            if view.proposal.status != "accepted":
                raise InvalidTransition()
            if view.accepted_review_id != review_id:
                raise ReviewConflict()
            self._insert_intent(conn, identity, view)
            return self._result(conn, identity, proposal_id)

    def _begin_apply_attempt(self, identity, proposal_id) -> None:
        with self._connect(self.settings) as conn, conn.transaction():
            self.reviews._view(conn, identity, proposal_id)
            apply = self._apply(conn, proposal_id, lock=True)
            if apply is None:
                raise ReviewMissing()
            if apply.refresh_proposal_id is not None:
                raise InvalidTransition()
            if apply.apply_status != ApplyStatus.APPLIED:
                conn.execute(
                    """UPDATE proposal_applies SET apply_attempts=apply_attempts+1,
                    last_apply_attempt_at=now() WHERE id=%s""",
                    (apply.id,),
                )

    def execute_apply(
        self,
        identity: ClientIdentity,
        proposal_id: UUID,
        writer: SecureKnowledgeWriter,
    ) -> ApplyView:
        self._begin_apply_attempt(identity, proposal_id)
        with self._connect(self.settings) as conn, conn.transaction():
            view = self.reviews._view(conn, identity, proposal_id)
            apply = self._apply(conn, proposal_id, lock=True)
            if apply is None or view.revision is None:
                raise ReviewMissing()
            if apply.review_id != view.accepted_review_id or apply.review_id != view.revision.id:
                raise InconsistentReview()
            if apply.apply_status == ApplyStatus.APPLIED:
                return self._result(conn, identity, proposal_id)
            if apply.refresh_proposal_id is not None:
                raise InvalidTransition()
            # Row lock -> shared full-index lock -> canonical document lock.
            # Every apply/index path uses this order; full indexing takes only
            # the exclusive global lock before reading its source snapshot.
            lock_document_index(conn, apply.target_source_id, apply.target_source_path)
            # Writers additionally coordinate their shared parent namespace;
            # no caller takes this lock before a row/global/document lock.
            lock_parent_write(conn, apply.target_source_id, apply.target_source_path)
            try:
                result = writer.apply(apply.id, view.revision)
            except KnowledgeConflict as exc:
                conn.execute(
                    """UPDATE proposal_applies SET apply_status='conflict', actual_hash=NULL,
                    apply_error_class=%s WHERE id=%s""",
                    (str(exc), apply.id),
                )
                LOG.warning("proposal_apply_conflict class=%s", str(exc))
            except KnowledgeIOFailure as exc:
                conn.execute(
                    """UPDATE proposal_applies SET apply_status='failed', actual_hash=NULL,
                    apply_error_class=%s WHERE id=%s""",
                    (str(exc), apply.id),
                )
                LOG.error("proposal_apply_failed class=%s", str(exc))
            else:
                if result.sha256 != apply.expected_new_hash:
                    raise InconsistentReview()
                conn.execute(
                    """UPDATE proposal_applies SET apply_status='applied',
                    actual_hash=%s, apply_error_class=NULL,
                    applied_at=COALESCE(applied_at,now()) WHERE id=%s""",
                    (result.sha256, apply.id),
                )
                LOG.info("proposal_apply_completed wrote=%s", result.wrote)
            return self._result(conn, identity, proposal_id)

    def execute_index(
        self,
        identity: ClientIdentity,
        proposal_id: UUID,
        source: GitMarkdownSource,
        index: DocumentIndexer,
    ) -> ApplyView:
        validate_document_indexer(index)
        self._begin_index_attempt(identity, proposal_id)
        with self._connect(self.settings) as conn, conn.transaction():
            view = self.reviews._view(conn, identity, proposal_id)
            apply = self._apply(conn, proposal_id, lock=True)
            if apply is None or view.revision is None:
                raise ReviewMissing()
            if apply.apply_status != ApplyStatus.APPLIED:
                raise InvalidTransition()
            if apply.index_status == IndexStatus.INDEXED:
                return self._result(conn, identity, proposal_id)
            lock_document_index(conn, apply.target_source_id, apply.target_source_path)
            try:
                normalized, current = source.snapshot(
                    apply.target_source_id, apply.target_source_path
                )
                if current is None or content_hash(current) != apply.expected_new_hash:
                    raise KnowledgeConflict("applied_source_changed")
                result = index(
                    SourceDocument(
                        apply.target_source_id, normalized, current, {"path": normalized}
                    ),
                    coordination=DocumentIndexCoordination.LOCKS_HELD,
                )
                validate_document_index_result(result)
            except DocumentIndexerContractError:
                conn.execute(
                    """UPDATE proposal_applies SET index_status='failed',
                    index_error_class='index_callback_contract' WHERE id=%s""",
                    (apply.id,),
                )
                LOG.error("proposal_index_failed class=index_callback_contract")
            except KnowledgeConflict as exc:
                conn.execute(
                    """UPDATE proposal_applies SET index_status='failed',
                    index_error_class=%s WHERE id=%s""",
                    (str(exc), apply.id),
                )
                LOG.warning("proposal_index_failed class=%s", str(exc))
            except Exception as exc:  # noqa: BLE001 - persist only the safe class category
                conn.execute(
                    """UPDATE proposal_applies SET index_status='failed',
                    index_error_class='index_operation_failed' WHERE id=%s""",
                    (apply.id,),
                )
                LOG.error("proposal_index_failed error=%s", type(exc).__name__)
            else:
                conn.execute(
                    """UPDATE proposal_applies SET index_status='indexed',
                    index_error_class=NULL, indexed_at=COALESCE(indexed_at,now()) WHERE id=%s""",
                    (apply.id,),
                )
                LOG.info("proposal_index_completed")
            return self._result(conn, identity, proposal_id)

    def _begin_index_attempt(self, identity: ClientIdentity, proposal_id: UUID) -> None:
        with self._connect(self.settings) as conn, conn.transaction():
            self.reviews._view(conn, identity, proposal_id)
            apply = self._apply(conn, proposal_id, lock=True)
            if apply is None:
                raise ReviewMissing()
            if apply.apply_status != ApplyStatus.APPLIED:
                raise InvalidTransition()
            if apply.index_status != IndexStatus.INDEXED:
                conn.execute(
                    """UPDATE proposal_applies SET index_attempts=index_attempts+1,
                    last_index_attempt_at=now() WHERE id=%s""",
                    (apply.id,),
                )

    def ensure_refresh_proposal(self, identity: ClientIdentity, proposal_id: UUID) -> UUID:
        """Create or reuse the one pending successor for an irreconcilable conflict."""
        with self._connect(self.settings) as conn, conn.transaction():
            self.reviews._view(conn, identity, proposal_id)
            apply = self._apply(conn, proposal_id, lock=True)
            if apply is None or apply.apply_status != ApplyStatus.CONFLICT:
                raise InvalidTransition()
            if apply.refresh_proposal_id is not None:
                return apply.refresh_proposal_id
            replacement = uuid4()
            trigger_message_id = conn.execute(
                """INSERT INTO messages (conversation_id, role, content, metadata)
                SELECT conversation_id, 'system',
                       'Browser refresh after an apply conflict',
                       '{"control": true, "kind": "apply_conflict_refresh"}'::jsonb
                  FROM proposals WHERE id=%s
                RETURNING id""",
                (proposal_id,),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO proposals
                (id, conversation_id, status, source_client, trigger_type,
                 trigger_message_id, originating_message_ids, target_source_id,
                 target_source_path, title, summary, reason, proposed_content,
                 base_revision, source_suggestion_id)
                SELECT %s, p.conversation_id, 'pending', p.source_client, p.trigger_type,
                       %s, p.originating_message_ids, r.target_source_id,
                       r.target_source_path, p.title, p.summary, p.reason, r.new_content,
                       r.base_revision, NULL
                  FROM proposals p
                  JOIN proposal_reviews r ON r.proposal_id=p.id AND r.id=%s
                 WHERE p.id=%s""",
                (replacement, trigger_message_id, apply.review_id, proposal_id),
            )
            conn.execute(
                "UPDATE proposal_applies SET refresh_proposal_id=%s WHERE id=%s",
                (replacement, apply.id),
            )
            return replacement


class ProposalApplyService:
    def __init__(
        self,
        settings: Settings,
        store: PostgresApplyStore | None = None,
        source: GitMarkdownSource | None = None,
        writer: SecureKnowledgeWriter | None = None,
        document_indexer: DocumentIndexer | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or PostgresApplyStore(settings)
        self.source = source or GitMarkdownSource(settings.knowledge_root)
        self.writer = writer or SecureKnowledgeWriter(settings.knowledge_root)
        self.document_indexer = (
            partial(index_document, settings) if document_indexer is None else document_indexer
        )
        validate_document_indexer(self.document_indexer)

    def get(self, identity: ClientIdentity, proposal_id: UUID) -> ApplyView:
        return self.store.get(identity, proposal_id)

    def accept_and_apply(self, identity, proposal_id, review_id, expected_status) -> ApplyView:
        index = self._validated_indexer()
        self.store.accept_and_intent(identity, proposal_id, review_id, expected_status)
        return self._apply_then_index(identity, proposal_id, index)

    def apply_accepted(self, identity, proposal_id, review_id) -> ApplyView:
        index = self._validated_indexer()
        self.store.ensure_intent(identity, proposal_id, review_id)
        return self._apply_then_index(identity, proposal_id, index)

    def retry_apply(self, identity, proposal_id) -> ApplyView:
        return self._apply_then_index(identity, proposal_id, self._validated_indexer())

    def retry_index(self, identity, proposal_id) -> ApplyView:
        return self.store.execute_index(
            identity, proposal_id, self.source, self._validated_indexer()
        )

    def refresh_review(self, identity, proposal_id) -> ReviewView:
        replacement = self.store.ensure_refresh_proposal(identity, proposal_id)
        reviews = ProposalReviewService(self.store.reviews, self.source)
        return reviews.prepare(identity, replacement)

    def _validated_indexer(self) -> DocumentIndexer:
        # Capture and validate at the mutation boundary as well as construction:
        # replacing an injected dependency must not defer errors until after apply.
        index = self.document_indexer
        validate_document_indexer(index)
        return index

    def _apply_then_index(self, identity, proposal_id, index: DocumentIndexer) -> ApplyView:
        result = self.store.execute_apply(identity, proposal_id, self.writer)
        if result.apply and result.apply.apply_status == ApplyStatus.APPLIED:
            result = self.store.execute_index(identity, proposal_id, self.source, index)
        return result
