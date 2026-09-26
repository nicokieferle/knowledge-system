from __future__ import annotations

from pathlib import Path

from knowledge_system.config import Settings
from knowledge_system.conversation_schema import init_conversation_schema
from knowledge_system.db import init_db


class RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, query: str, params=None) -> None:
        self.statements.append(query)


def test_durable_schema_is_additive_and_has_required_consistency_constraints() -> None:
    connection = RecordingConnection()

    init_conversation_schema(connection)

    sql = "\n".join(connection.statements).lower()
    assert "create table if not exists conversations" in sql
    assert "create table if not exists client_states" in sql
    assert "create table if not exists client_conversations" in sql
    assert "create table if not exists client_message_bindings" in sql
    assert "create table if not exists messages" in sql
    assert "create table if not exists conversation_summaries" in sql
    assert "create table if not exists proposal_suggestions" in sql
    assert "create table if not exists proposals" in sql
    assert "source_suggestion_id uuid unique" in sql
    assert "unique (conversation_id, trigger_message_id, trigger_type)" in sql
    assert "delete from " not in sql
    assert "truncate " not in sql
    assert "drop table" not in sql
    assert "chunks" not in sql

    review_schema_calls = [
        statement
        for statement in connection.statements
        if "pg_advisory_xact_lock(320032)" in statement
    ]
    assert len(review_schema_calls) == 1
    assert "proposal_reviews_immutable" in review_schema_calls[0]
    assert "proposals_terminal" in review_schema_calls[0]


def test_database_initialization_uses_additive_durable_schema(monkeypatch) -> None:
    class ConnectionContext(RecordingConnection):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    connection = ConnectionContext()
    monkeypatch.setattr("knowledge_system.db.psycopg.connect", lambda *args, **kwargs: connection)
    monkeypatch.setattr("knowledge_system.db.register_vector", lambda conn: None)

    init_db(Settings("postgresql://example", Path("knowledge"), "model", 384))

    sql = "\n".join(connection.statements).lower()
    assert "create table if not exists conversations" in sql
    assert "create table if not exists proposals" in sql
    for table in (
        "conversations",
        "client_states",
        "client_conversations",
        "client_message_bindings",
        "messages",
        "conversation_summaries",
        "proposal_suggestions",
        "proposals",
    ):
        related = [statement for statement in connection.statements if table in statement.lower()]
        assert related
        assert all(
            keyword not in statement.lower()
            for statement in related
            for keyword in ("delete from", "truncate", "drop table")
        )
