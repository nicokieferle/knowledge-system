from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any

from knowledge_system.chunking import chunk_markdown_text
from knowledge_system.config import Settings
from knowledge_system.indexer import index_document, index_knowledge
from knowledge_system.sources import SourceDocument


class FakeSource:
    source_id = "test-source"

    def __init__(self) -> None:
        self.discovered = False

    def discover(self) -> list[SourceDocument]:
        self.discovered = True
        return [
            SourceDocument(
                source_id=self.source_id,
                source_path="economics/test.md",
                content="# Test\n\nContent.",
                metadata={},
            )
        ]

    def get_document(self, source_path: str) -> SourceDocument:
        raise NotImplementedError


class FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.calls.append((sql, params))

    @contextmanager
    def transaction(self):
        yield


class FakeEmbedder:
    def encode(self, values):
        return [[float(index), 1.0] for index, _ in enumerate(values)]


def _settings() -> Settings:
    return Settings(
        database_url="postgresql://example",
        knowledge_root=Path("knowledge"),
        embedding_model="model",
        embedding_dimensions=384,
    )


def test_indexer_uses_source_adapter(monkeypatch, capsys) -> None:
    source = FakeSource()
    chunks = chunk_markdown_text("# Test\n\nContent.", "economics/test.md")
    fake_conn = FakeConnection()

    def fake_load_existing_chunk_state(settings: Settings, source_id: str):
        assert source_id == "test-source"
        return {chunk.chunk_key: (chunk.content_hash, settings.embedding_model) for chunk in chunks}

    @contextmanager
    def fake_connect(settings: Settings):
        yield fake_conn

    monkeypatch.setattr(
        "knowledge_system.indexer._load_existing_chunk_state",
        fake_load_existing_chunk_state,
    )
    monkeypatch.setattr("knowledge_system.indexer.connect", fake_connect)

    index_knowledge(_settings(), source=source)

    assert source.discovered is True
    assert "[index] Found 1 document(s) from source test-source" in capsys.readouterr().out
    assert any(
        "DELETE FROM chunks WHERE source_id" in sql and params[0] == "test-source"
        for sql, params in fake_conn.calls
    )
    durable_tables = (
        "conversations",
        "client_states",
        "client_conversations",
        "client_message_bindings",
        "messages",
        "conversation_summaries",
        "proposal_suggestions",
        "proposals",
    )
    assert all(
        durable_table not in sql.lower()
        for sql, _ in fake_conn.calls
        for durable_table in durable_tables
    )


def test_document_index_replaces_only_one_path_in_one_transaction() -> None:
    connection = FakeConnection()

    @contextmanager
    def factory(settings):
        yield connection

    document = SourceDocument(
        "knowledge-git",
        "economics/test.md",
        "# First\n\nOne.\n\n# Second\n\nTwo.",
        {"path": "economics/test.md"},
    )
    count = index_document(
        _settings(), document, embedder=FakeEmbedder(), connection_factory=factory
    )

    assert count == 2
    assert "DELETE FROM chunks WHERE source_id=%s AND source_path=%s" in connection.calls[0][0]
    assert connection.calls[0][1] == ("knowledge-git", "economics/test.md")
    inserts = [call for call in connection.calls if "INSERT INTO chunks" in call[0]]
    assert len(inserts) == 2
    assert {call[1][4] for call in inserts} == {0, 1}
    assert all(call[1][2] == "economics/test.md" for call in inserts)
