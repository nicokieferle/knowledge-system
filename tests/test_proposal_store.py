from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from knowledge_system.config import Settings
from knowledge_system.conversation_models import ProposalDraft, ProposalTriggerType
from knowledge_system.proposal_store import PostgresProposalStore


class Cursor:
    def __init__(self, row) -> None:
        self.row = row

    def fetchone(self):
        return self.row


class RecordingConnection:
    def __init__(self, suggestion_row, proposal_row) -> None:
        self.suggestion_row = suggestion_row
        self.proposal_row = proposal_row
        self.calls: list[tuple[str, tuple]] = []
        self.transaction_count = 0

    @contextmanager
    def transaction(self):
        self.transaction_count += 1
        yield

    def execute(self, query: str, params: tuple = ()) -> Cursor:
        self.calls.append((query, params))
        normalized = " ".join(query.split()).lower()
        if "from proposal_suggestions" in normalized and "for update" in normalized:
            return Cursor(self.suggestion_row)
        if normalized.startswith("insert into proposals"):
            return Cursor(self.proposal_row)
        if normalized.startswith("update proposal_suggestions"):
            return Cursor((self.suggestion_row[0],))
        raise AssertionError(normalized)


def test_postgres_confirmation_uses_one_transaction_lock_and_conditional_update() -> None:
    conversation_id = uuid4()
    suggestion_id = uuid4()
    now = datetime.now(UTC)
    suggestion_row = (
        suggestion_id,
        conversation_id,
        10,
        [7, 8],
        "summary",
        "pending",
        now,
        None,
        None,
    )
    proposal_id = uuid4()
    proposal_row = (
        proposal_id,
        conversation_id,
        "pending",
        "api",
        "confirmed_suggestion",
        12,
        [7, 8, 10],
        "draft summary",
        "reason",
        "content",
        "title",
        None,
        None,
        None,
        suggestion_id,
        now,
    )
    connection = RecordingConnection(suggestion_row, proposal_row)

    @contextmanager
    def connection_factory(settings):
        yield connection

    settings = Settings("postgresql://example", Path("knowledge"), "model", 384)
    store = PostgresProposalStore(settings, connection_factory=connection_factory)

    proposal = store.confirm_suggestion(
        conversation_id,
        suggestion_id,
        confirmation_message_id=12,
        source_client="api",
        draft=ProposalDraft(
            title="title",
            summary="draft summary",
            reason="reason",
            proposed_content="content",
        ),
    )

    sql = [" ".join(query.split()).lower() for query, _ in connection.calls]
    assert connection.transaction_count == 1
    assert "for update" in sql[0]
    assert sql[1].startswith("insert into proposals")
    assert "source_suggestion_id" in sql[1]
    assert sql[2].startswith("update proposal_suggestions")
    assert "status = 'pending'" in sql[2]
    assert proposal.id == proposal_id
    assert proposal.trigger_type is ProposalTriggerType.CONFIRMED_SUGGESTION
