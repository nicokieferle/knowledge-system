from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from knowledge_system.config import Settings
from knowledge_system.durable_state import DURABLE_TABLES, read_durable_state_fingerprint


class Cursor:
    def __init__(self, rows) -> None:
        self.rows = rows

    def fetchall(self):
        return self.rows


class FingerprintConnection:
    def __init__(self, rows_by_table) -> None:
        self.rows_by_table = rows_by_table

    def execute(self, query: str) -> Cursor:
        table = next(table for table in DURABLE_TABLES if f"FROM {table}" in query)
        return Cursor(self.rows_by_table[table])


def _settings() -> Settings:
    return Settings("postgresql://example", Path("knowledge"), "unused", 384)


def test_durable_fingerprint_is_deterministic_and_counts_all_tables(monkeypatch) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    conversation_id = uuid4()
    rows = {
        "conversations": [(conversation_id, "Private title", "api", "external", now, now, None)],
        "client_states": [
            ("telegram", "private-chat", "private-user", conversation_id, 1, now, now)
        ],
        "client_conversations": [
            ("telegram", "private-chat", "private-user", conversation_id, now)
        ],
        "client_message_bindings": [
            ("telegram", "private-chat", "private-user", "private-message", conversation_id, now)
        ],
        "messages": [(1, conversation_id, "user", "Private text", now, "m-1", {"x": 1})],
        "conversation_summaries": [],
        "proposal_suggestions": [],
        "proposals": [],
    }

    @contextmanager
    def fake_connect(settings):
        yield FingerprintConnection(rows)

    monkeypatch.setattr("knowledge_system.durable_state.connect", fake_connect)

    first = read_durable_state_fingerprint(_settings())
    second = read_durable_state_fingerprint(_settings())

    assert first == second
    assert first.counts == {
        "conversations": 1,
        "client_states": 1,
        "client_conversations": 1,
        "client_message_bindings": 1,
        "messages": 1,
        "conversation_summaries": 0,
        "proposal_suggestions": 0,
        "proposals": 0,
    }
    assert len(first.sha256) == 64
    assert "Private" not in first.sha256


def test_durable_fingerprint_detects_private_content_change_without_exposing_it(
    monkeypatch,
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    conversation_id = uuid4()
    rows = {
        "conversations": [(conversation_id, None, "api", None, now, now, None)],
        "client_states": [
            ("telegram", "private-chat", "private-user", conversation_id, 1, now, now)
        ],
        "client_conversations": [
            ("telegram", "private-chat", "private-user", conversation_id, now)
        ],
        "client_message_bindings": [
            ("telegram", "private-chat", "private-user", "private-message", conversation_id, now)
        ],
        "messages": [(1, conversation_id, "user", "First private text", now, None, {})],
        "conversation_summaries": [],
        "proposal_suggestions": [],
        "proposals": [],
    }

    @contextmanager
    def fake_connect(settings):
        yield FingerprintConnection(rows)

    monkeypatch.setattr("knowledge_system.durable_state.connect", fake_connect)
    before = read_durable_state_fingerprint(_settings())
    rows["messages"][0] = (
        1,
        conversation_id,
        "user",
        "Changed private text",
        now,
        None,
        {},
    )
    after = read_durable_state_fingerprint(_settings())

    assert before.sha256 != after.sha256
    assert "private" not in before.sha256.lower()
    assert "private" not in after.sha256.lower()


def test_durable_fingerprint_detects_client_external_id_changes_without_exposing_them(
    monkeypatch,
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    conversation_id = uuid4()
    rows = {
        "conversations": [(conversation_id, None, "telegram", None, now, now, None)],
        "client_states": [
            ("telegram", "private-chat", "private-user", conversation_id, 1, now, now)
        ],
        "client_conversations": [
            ("telegram", "private-chat", "private-user", conversation_id, now)
        ],
        "client_message_bindings": [
            ("telegram", "private-chat", "private-user", "private-message", conversation_id, now)
        ],
        "messages": [],
        "conversation_summaries": [],
        "proposal_suggestions": [],
        "proposals": [],
    }

    @contextmanager
    def fake_connect(settings):
        yield FingerprintConnection(rows)

    monkeypatch.setattr("knowledge_system.durable_state.connect", fake_connect)
    before = read_durable_state_fingerprint(_settings())
    rows["client_message_bindings"][0] = (
        "telegram",
        "private-chat",
        "private-user",
        "changed-private-message",
        conversation_id,
        now,
    )
    after = read_durable_state_fingerprint(_settings())

    assert before.sha256 != after.sha256
    assert before.counts["client_message_bindings"] == 1
    assert "private" not in before.sha256.lower()
    assert "private" not in after.sha256.lower()
