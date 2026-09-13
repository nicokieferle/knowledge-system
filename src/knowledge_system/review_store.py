from __future__ import annotations

import logging
from uuid import UUID, uuid4

from .client_state import ClientIdentity
from .config import Settings
from .db import connect
from .proposal_review import (
    InconsistentReview,
    InvalidTransition,
    ProposalNotFound,
    ProposalStatus,
    ReviewConflict,
    ReviewContent,
    ReviewContext,
    ReviewForbidden,
    ReviewMissing,
    ReviewPage,
    ReviewQueueItem,
    ReviewRevision,
    ReviewView,
    content_hash,
    unified_diff,
)
from .proposal_store import _PROPOSAL_SELECT, _proposal_from_row

LOG = logging.getLogger(__name__)
REVIEW_SELECT = """SELECT id, proposal_id, revision, target_source_id, target_source_path,
 change_kind, base_revision, old_content, old_hash, new_content, new_hash, diff, created_at
 FROM proposal_reviews"""


def revision_from_row(row):
    if row is None:
        return None
    c = ReviewContent(*row[3:12])
    if (
        c.new_hash != content_hash(c.new_content)
        or c.old_hash != (content_hash(c.old_content) if c.old_content is not None else None)
        or c.diff != unified_diff(c.target_source_path, c.old_content, c.new_content)
    ):
        raise InconsistentReview()
    return ReviewRevision(row[0], row[1], row[2], c, row[12])


class PostgresReviewStore:
    def __init__(self, settings: Settings, connection_factory=connect):
        self.settings, self._connect = settings, connection_factory

    @staticmethod
    def _authorize(conn, identity: ClientIdentity, conversation_id: UUID):
        # No recent-topic limit. Hold the ownership row through the decision commit.
        row = conn.execute(
            """SELECT conversation_id FROM client_conversations
            WHERE client_type=%s AND external_chat_id=%s AND external_user_id=%s
              AND conversation_id=%s FOR SHARE""",
            (
                identity.client_type,
                identity.external_chat_id,
                identity.external_user_id,
                conversation_id,
            ),
        ).fetchone()
        if row is None:
            LOG.warning("review_ownership_denied")
            raise ReviewForbidden()

    def _view(self, conn, identity, proposal_id):
        row = conn.execute(_PROPOSAL_SELECT + " WHERE id=%s FOR UPDATE", (proposal_id,)).fetchone()
        if row is None:
            raise ProposalNotFound()
        proposal = _proposal_from_row(row)
        self._authorize(conn, identity, proposal.conversation_id)
        revision = revision_from_row(
            conn.execute(
                REVIEW_SELECT + " WHERE proposal_id=%s ORDER BY revision DESC LIMIT 1",
                (proposal_id,),
            ).fetchone()
        )
        accepted = conn.execute(
            "SELECT accepted_review_id FROM proposals WHERE id=%s", (proposal_id,)
        ).fetchone()[0]
        if (proposal.status == "accepted") != (accepted is not None):
            raise InconsistentReview()
        if accepted and (revision is None or accepted != revision.id):
            raise InconsistentReview()
        if proposal.status != "pending":
            decision = conn.execute(
                "SELECT review_id FROM proposal_decisions WHERE proposal_id=%s AND status=%s",
                (proposal_id, proposal.status),
            ).fetchone()
            if decision is None or proposal.status == "accepted" and decision[0] != accepted:
                raise InconsistentReview()
        return ReviewView(proposal, revision, accepted)

    def get(self, identity, proposal_id):
        with self._connect(self.settings) as conn, conn.transaction():
            return self._view(conn, identity, proposal_id)

    def context(self, identity, proposal_id):
        with self._connect(self.settings) as conn, conn.transaction():
            p = self._view(conn, identity, proposal_id).proposal
            # Originating messages exclude command/confirmation control input. Never
            # resolve IDs outside the authorized conversation, even for legacy arrays.
            rows = conn.execute(
                """SELECT id, content FROM messages WHERE conversation_id=%s
                AND id=ANY(%s) AND role='user' AND NOT (metadata ? 'control')
                ORDER BY id""",
                (p.conversation_id, list(p.originating_message_ids)),
            ).fetchall()
            return tuple(ReviewContext(*row) for row in rows)

    def queue(self, identity, statuses, offset, limit):
        owned = """EXISTS (SELECT 1 FROM client_conversations cc
            WHERE cc.conversation_id=p.conversation_id AND cc.client_type=%s
            AND cc.external_chat_id=%s AND cc.external_user_id=%s)"""
        key = (identity.client_type, identity.external_chat_id, identity.external_user_id)
        with self._connect(self.settings) as conn, conn.transaction():
            # Counts and page share one snapshot; GET never creates a review.
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            total, open_count = conn.execute(
                "SELECT count(*) FILTER (WHERE status=ANY(%s)), "
                "count(*) FILTER (WHERE status IN ('pending','deferred')) "
                "FROM proposals p WHERE " + owned,
                (list(statuses), *key),
            ).fetchone()
            rows = conn.execute(
                """SELECT p.id, p.status, p.created_at,
                COALESCE(r.target_source_path,p.target_source_path),
                (SELECT left(m.content,240) FROM messages m
                 WHERE m.conversation_id=p.conversation_id
                 AND m.id=ANY(p.originating_message_ids) AND m.role='user'
                 AND NOT (m.metadata ? 'control') ORDER BY m.id DESC LIMIT 1), r.revision
                FROM proposals p LEFT JOIN LATERAL
                (SELECT revision,target_source_path FROM proposal_reviews
                 WHERE proposal_id=p.id ORDER BY revision DESC LIMIT 1) r ON true
                WHERE """
                + owned
                + " AND p.status=ANY(%s) "
                "ORDER BY p.created_at DESC,p.id DESC LIMIT %s OFFSET %s",
                (*key, list(statuses), limit, offset),
            ).fetchall()
            return ReviewPage(tuple(ReviewQueueItem(*row) for row in rows), total, open_count)

    def proposal_for_revision(self, identity, review_id):
        with self._connect(self.settings) as conn, conn.transaction():
            row = conn.execute(
                "SELECT proposal_id FROM proposal_reviews WHERE id=%s", (review_id,)
            ).fetchone()
            if row is None:
                raise ReviewMissing()
            self._view(conn, identity, row[0])
            return row[0]

    def list(self, identity, conversation_id):
        with self._connect(self.settings) as conn, conn.transaction():
            self._authorize(conn, identity, conversation_id)
            rows = conn.execute(
                _PROPOSAL_SELECT
                + """ WHERE conversation_id=%s
                AND status IN ('pending', 'deferred') ORDER BY created_at, id LIMIT 100""",
                (conversation_id,),
            ).fetchall()
            return tuple(_proposal_from_row(row) for row in rows)

    def prepare(self, identity, proposal_id, content: ReviewContent, expected_review_id):
        with self._connect(self.settings) as conn, conn.transaction():
            view = self._view(conn, identity, proposal_id)
            if view.proposal.status not in ("pending", "deferred"):
                raise InvalidTransition()
            if view.revision and view.revision.content == content:
                LOG.info("review_prepare_idempotent")
                return view
            if (view.revision.id if view.revision else None) != expected_review_id:
                raise ReviewConflict()
            number = view.revision.number + 1 if view.revision else 1
            conn.execute(
                """INSERT INTO proposal_reviews
                (id, proposal_id, revision, target_source_id, target_source_path, change_kind,
                 base_revision, old_content, old_hash, new_content, new_hash, diff)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    uuid4(),
                    proposal_id,
                    number,
                    content.target_source_id,
                    content.target_source_path,
                    content.change_kind,
                    content.base_revision,
                    content.old_content,
                    content.old_hash,
                    content.new_content,
                    content.new_hash,
                    content.diff,
                ),
            )
            result = self._view(conn, identity, proposal_id)
        LOG.info("review_prepared revision=%d", number)
        return result

    def decide(self, identity, proposal_id, review_id, status, expected_status):
        status = ProposalStatus(status)
        if status is ProposalStatus.PENDING:
            raise InvalidTransition()
        with self._connect(self.settings) as conn, conn.transaction():
            view = self._view(conn, identity, proposal_id)
            current_id = view.revision.id if view.revision else None
            if view.proposal.status == status:
                recorded = conn.execute(
                    """SELECT review_id FROM proposal_decisions
                    WHERE proposal_id=%s AND status=%s""",
                    (proposal_id, status.value),
                ).fetchone()
                if recorded is None:
                    raise InconsistentReview()
                if recorded[0] != review_id:
                    raise ReviewConflict()
                LOG.info("review_decision_idempotent status=%s", status.value)
                return view
            if view.proposal.status in ("accepted", "rejected"):
                LOG.warning("review_transition_rejected")
                raise InvalidTransition()
            if view.proposal.status != expected_status:
                raise ReviewConflict()
            if current_id != review_id:
                raise ReviewConflict()
            if status is ProposalStatus.ACCEPTED and review_id is None:
                raise ReviewMissing()
            conn.execute(
                """INSERT INTO proposal_decisions
                (id, proposal_id, review_id, previous_status, status,
                 client_type, external_chat_id, external_user_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    uuid4(),
                    proposal_id,
                    review_id,
                    view.proposal.status,
                    status.value,
                    identity.client_type,
                    identity.external_chat_id,
                    identity.external_user_id,
                ),
            )
            conn.execute(
                "UPDATE proposals SET status=%s, accepted_review_id=%s WHERE id=%s",
                (
                    status.value,
                    review_id if status is ProposalStatus.ACCEPTED else None,
                    proposal_id,
                ),
            )
            result = self._view(conn, identity, proposal_id)
        LOG.info("review_status_changed status=%s", status.value)
        return result
