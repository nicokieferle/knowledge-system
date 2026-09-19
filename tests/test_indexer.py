from __future__ import annotations

import gc
import inspect
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import replace
from functools import partial, wraps
from pathlib import Path
from types import AsyncGeneratorType, CoroutineType, GeneratorType
from typing import Any

import pytest

from knowledge_system.chunking import chunk_markdown_text
from knowledge_system.config import Settings
from knowledge_system.indexer import (
    DocumentIndexCoordination,
    index_document,
    index_knowledge,
    validate_document_index_result,
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


def test_partial_subclass_override_uses_its_actual_bound_signature():
    def backing(document, *, coordination):
        return 1

    class Incompatible(partial):
        def __call__(self, document):
            raise AssertionError("signature validation must precede execution")

    class Compatible(partial):
        def __call__(self, document, *, coordination):
            return self.func(document, coordination=coordination)

    class PositionalOrKeyword(partial):
        def __call__(self, document, coordination):
            return self.func(document, coordination=coordination)

    with pytest.raises(TypeError, match="keyword-only coordination"):
        validate_document_indexer(Incompatible(backing))
    validate_document_indexer(Compatible(backing))
    validate_document_indexer(PositionalOrKeyword(backing))


def test_partial_and_inherited_partial_subclass_keep_partial_binding_rules():
    def callback(prefix, document, *, coordination):
        return prefix, document, coordination

    class Inherited(partial):
        pass

    exact = partial(callback, "exact")
    inherited = Inherited(callback, "inherited")
    nested = partial(partial(callback, "nested"))
    wrapped = wraps(callback)(partial(callback, "wrapped"))
    for index in (exact, inherited, nested, wrapped):
        validate_document_indexer(index)
        assert index(object(), coordination=DocumentIndexCoordination.LOCKS_HELD)[2] == "locks_held"


def test_partial_of_bound_method_and_falsey_compatible_override_are_supported():
    class Indexer:
        def run(self, prefix, document, *, coordination):
            return prefix, coordination

    class Falsey(partial):
        def __bool__(self):
            return False

        def __call__(self, document, *, coordination):
            return partial.__call__(self, document, coordination=coordination)

    bound = partial(Indexer().run, "bound")
    falsey = Falsey(bound)
    validate_document_indexer(bound)
    validate_document_indexer(falsey)
    assert falsey(object(), coordination=DocumentIndexCoordination.LOCKS_HELD) == (
        "bound",
        "locks_held",
    )


def test_partial_subclass_checks_override_and_backing_execution_modes():
    async def async_backing(document, *, coordination):
        raise AssertionError("must never execute")

    def sync_backing(document, *, coordination):
        return None

    class SyncOverride(partial):
        def __call__(self, document, *, coordination):
            return self.func(document, coordination=coordination)

    class AsyncOverride(partial):
        async def __call__(self, document, *, coordination):
            raise AssertionError("must never execute")

    for index in (SyncOverride(async_backing), AsyncOverride(sync_backing)):
        with pytest.raises(TypeError, match="document_indexer must be synchronous"):
            validate_document_indexer(index)


def test_callback_introspection_graph_is_identity_cycle_safe():
    def first(document, *, coordination):
        return None

    def second(document, *, coordination):
        return None

    first.__wrapped__ = second
    second.__wrapped__ = first
    validate_document_indexer(first)


@pytest.mark.parametrize(
    ("native_type", "method"),
    [
        (CoroutineType, "close"),
        (GeneratorType, "close"),
        (AsyncGeneratorType, "aclose"),
    ],
)
def test_deferred_type_proxies_never_dispatch_foreign_cleanup(native_type, method):
    called = []

    class Proxy:
        __class__ = native_type

        def close(self):
            called.append("close")

        def aclose(self):
            called.append("aclose")

    with pytest.raises(TypeError, match="after synchronous completion"):
        validate_document_index_result(Proxy())
    assert called == [], method


DEFERRED_KINDS = ("coroutine", "generator", "async_generator")
DEFERRED_FORMS = (
    "function",
    "partial",
    "bound",
    "callable",
    "wrapper",
    "nested",
    "wrapped_partial",
    "partial_callable",
    "wrapped_bound",
    "partial_subclass",
)


def deferred_callback(kind, form):
    """Inspectable deferred code, including forwarding wrappers and bound methods."""
    if kind == "coroutine":

        async def target(document, *, coordination):
            raise AssertionError("deferred body must not execute")

        async def method(self, document, *, coordination):
            raise AssertionError("deferred body must not execute")
    elif kind == "generator":

        def target(document, *, coordination):
            raise AssertionError("deferred body must not execute")
            yield

        def method(self, document, *, coordination):
            raise AssertionError("deferred body must not execute")
            yield
    else:

        async def target(document, *, coordination):
            raise AssertionError("deferred body must not execute")
            yield

        async def method(self, document, *, coordination):
            raise AssertionError("deferred body must not execute")
            yield

    if form == "partial_subclass":
        # The actual __call__ can differ from the partial's synchronous func.
        deferred_partial = type("DeferredPartial", (partial,), {"__call__": method})
        return deferred_partial(lambda document, *, coordination: None)
    if form in ("bound", "callable", "partial_callable", "wrapped_bound"):
        instance = type("DeferredIndexer", (), {"__call__": method})()
        target = instance.__call__ if form in ("bound", "wrapped_bound") else instance
    if form in ("partial", "partial_callable"):
        return partial(target)
    if form in ("wrapper", "nested", "wrapped_partial", "wrapped_bound"):

        @wraps(target)
        def wrapper(*args, **kwargs):
            return target(*args, **kwargs)

        if form == "nested":

            @wraps(wrapper)
            def outer(*args, **kwargs):
                return wrapper(*args, **kwargs)

            return partial(outer)
        return partial(wrapper) if form == "wrapped_partial" else wrapper
    return target


@pytest.mark.parametrize("kind", DEFERRED_KINDS)
@pytest.mark.parametrize("form", DEFERRED_FORMS)
def test_deferred_callbacks_are_rejected_at_construction(kind, form):
    with pytest.raises(TypeError, match="document_indexer must be synchronous"):
        ProposalApplyService(_settings(), document_indexer=deferred_callback(kind, form))


def deferred_result(kind, entered):
    if kind == "coroutine":

        async def work():
            entered.append("body")

        return work()
    if kind == "generator":

        def work():
            entered.append("body")
            yield

        return work()
    if kind == "async_generator":

        async def work():
            entered.append("body")
            yield

        return work()
    if kind == "awaitable":

        class Awaitable:
            def __await__(self):
                entered.append("await")
                yield

            def close(self):
                entered.append("foreign close")

        return Awaitable()
    if kind == "future":
        return Future()
    raise AssertionError(kind)


@pytest.mark.parametrize("kind", (*DEFERRED_KINDS, "awaitable", "future"))
def test_deferred_results_are_rejected_and_only_unstarted_native_objects_closed(kind, recwarn):
    entered = []
    result = deferred_result(kind, entered)
    with pytest.raises(TypeError, match="after synchronous completion"):
        validate_document_index_result(result)
    assert entered == []
    if kind == "coroutine":
        assert inspect.getcoroutinestate(result) == inspect.CORO_CLOSED
    elif kind == "generator":
        assert inspect.getgeneratorstate(result) == inspect.GEN_CLOSED
    elif kind == "async_generator":
        if hasattr(inspect, "getasyncgenstate"):
            assert inspect.getasyncgenstate(result) == inspect.AGEN_CLOSED
        else:
            assert result.ag_frame is not None
    elif kind == "future":
        assert not result.cancelled() and not result.done()
    del result
    gc.collect()
    assert not recwarn.list


@pytest.mark.parametrize("result", [None, 0, 42])
def test_only_synchronous_completion_results_are_valid(result):
    validate_document_index_result(result)


@pytest.mark.parametrize("result", [True, False, -1, 1.0, "1", [], object()])
def test_unknown_or_invalid_completion_results_are_rejected(result):
    with pytest.raises(TypeError, match="return None or a non-negative int"):
        validate_document_index_result(result)


def test_suspended_generator_is_not_closed_by_validation():
    entered = []

    def work():
        try:
            yield
        finally:
            entered.append("finally")

    result = work()
    next(result)
    try:
        with pytest.raises(TypeError, match="synchronous completion"):
            validate_document_index_result(result)
        assert entered == []
        assert inspect.getgeneratorstate(result) == inspect.GEN_SUSPENDED
    finally:
        result.close()
    assert entered == ["finally"]


def test_async_generator_without_state_inspection_is_rejected_without_driving_it(monkeypatch):
    monkeypatch.delattr(inspect, "getasyncgenstate", raising=False)
    entered = []
    result = deferred_result("async_generator", entered)
    try:
        with pytest.raises(TypeError, match="synchronous completion"):
            validate_document_index_result(result)
        assert result.ag_frame is not None and entered == []
    finally:
        # The test owns this known-unstarted object; production must not guess.
        with pytest.raises(StopIteration):
            result.aclose().send(None)
    assert entered == []
