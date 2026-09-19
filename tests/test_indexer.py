from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from functools import partial, wraps
from pathlib import Path
from typing import Any

import pytest

from knowledge_system.chunking import chunk_markdown_text
from knowledge_system.config import Settings
from knowledge_system.indexer import (
    DocumentIndexCoordination,
    index_document,
    index_knowledge,
    validate_document_indexer,
)
from knowledge_system.proposal_apply import ProposalApplyService
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

    def fake_load_existing_chunk_state(conn, source_id: str):
        assert conn is fake_conn
        assert source_id == "test-source"
        return {chunk.chunk_key: (chunk.content_hash, "model") for chunk in chunks}

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


def test_document_index_replaces_only_one_path_in_one_transaction(tmp_path) -> None:
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
    (tmp_path / "economics").mkdir()
    (tmp_path / document.source_path).write_text(document.content, encoding="utf-8")
    count = index_document(
        replace(_settings(), knowledge_root=tmp_path),
        document,
        coordination=DocumentIndexCoordination.ACQUIRE_LOCKS,
        embedder=FakeEmbedder(),
        connection_factory=factory,
    )

    assert count == 2
    delete = next(call for call in connection.calls if "DELETE FROM chunks" in call[0])
    assert delete[1] == ("knowledge-git", "economics/test.md")
    assert "pg_advisory_xact_lock_shared" in connection.calls[0][0]
    inserts = [call for call in connection.calls if "INSERT INTO chunks" in call[0]]
    assert len(inserts) == 2
    assert {call[1][4] for call in inserts} == {0, 1}
    assert all(call[1][2] == "economics/test.md" for call in inserts)


CALLBACK_FORMS = ("function", "wrapper", "partial", "wrapped_partial", "bound", "callable")


def callback_form(kind, compatible, callback=None):
    """Build actual calling interfaces independently of their wrapper metadata."""

    def good(document, *, coordination):
        if callback is not None:
            return callback(document, coordination=coordination)

    def old(document):
        raise AssertionError("An obsolete callback must never execute")

    target = good if compatible else old
    if kind in ("wrapper", "wrapped_partial"):
        if compatible:

            @wraps(good)
            def wrapper(*args, **kwargs):
                return good(*args, **kwargs)
        else:

            @wraps(good)
            def wrapper(document):
                return old(document)

        target = wrapper
    if kind in ("partial", "wrapped_partial"):
        return partial(target)
    if kind in ("bound", "callable"):
        if compatible:

            class Indexer:
                def __call__(self, document, *, coordination):
                    return good(document, coordination=coordination)
        else:

            class Indexer:
                def __call__(self, document):
                    return old(document)

        instance = Indexer()
        return instance.__call__ if kind == "bound" else instance
    return target


@pytest.mark.parametrize("kind", CALLBACK_FORMS)
@pytest.mark.parametrize("compatible", [False, True])
def test_callback_validation_checks_the_outer_callable(kind, compatible):
    index = callback_form(kind, compatible)
    if compatible:
        validate_document_indexer(index)
        service = ProposalApplyService(_settings(), document_indexer=index)
        assert service.document_indexer is index
        index(object(), coordination=DocumentIndexCoordination.LOCKS_HELD)
    else:
        with pytest.raises(
            TypeError, match="document_indexer must accept document and keyword-only coordination"
        ):
            ProposalApplyService(_settings(), document_indexer=index)


def test_falsey_incompatible_callback_cannot_silently_select_the_default():
    class Indexer:
        def __bool__(self):
            return False

        def __call__(self, document):
            raise AssertionError("must never execute")

    with pytest.raises(TypeError, match="keyword-only coordination"):
        ProposalApplyService(_settings(), document_indexer=Indexer())
