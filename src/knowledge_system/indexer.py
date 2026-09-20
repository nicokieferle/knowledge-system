from __future__ import annotations

import inspect
from enum import StrEnum
from functools import partial
from types import AsyncGeneratorType, CoroutineType, GeneratorType
from typing import Protocol

from pgvector import Vector

from .chunking import Chunk, chunk_markdown_text
from .config import Settings
from .db import connect
from .index_coordination import lock_document_index, lock_full_index
from .sources import GitMarkdownSource, SourceAdapter, SourceDocument

ExistingChunkState = tuple[str, str]
DocumentIndexResult = int | None


class DocumentIndexerContractError(TypeError):
    """An injected indexer did not satisfy the synchronous completion contract."""


class DocumentIndexCoordination(StrEnum):
    """Explicit lock state required by every document-index call."""

    ACQUIRE_LOCKS = "acquire_locks"
    LOCKS_HELD = "locks_held"


class DocumentIndexer(Protocol):
    """Complete indexing synchronously; return a chunk count or None, never deferred work."""

    def __call__(
        self,
        document: SourceDocument,
        *,
        coordination: DocumentIndexCoordination,
    ) -> DocumentIndexResult: ...


def _effective_indexer_signature(index: object) -> inspect.Signature:
    """Describe the interface Python will use after every partial binding."""

    if not isinstance(index, partial):
        return inspect.signature(index, follow_wrapped=False)

    call_descriptor = next(
        cls.__dict__["__call__"] for cls in type(index).__mro__ if "__call__" in cls.__dict__
    )
    if call_descriptor is not partial.__call__:
        signature_target = (
            call_descriptor.__get__(index, type(index))
            if hasattr(call_descriptor, "__get__")
            else call_descriptor
        )
        return inspect.signature(signature_target, follow_wrapped=False)

    # inspect.signature(partial_instance) normally derives the signature from
    # partial.func. Resolve that function ourselves so a nested partial subclass
    # cannot use misleading __wrapped__ metadata to hide its real __call__.
    inner_signature = _effective_indexer_signature(index.func)

    def signature_target(*args, **kwargs):
        raise AssertionError("signature-only callable must never execute")

    signature_target.__signature__ = inner_signature  # type: ignore[attr-defined]
    bound = partial(signature_target, *index.args, **(index.keywords or {}))
    return inspect.signature(bound, follow_wrapped=False)


def validate_document_indexer(index: object) -> None:
    """Reject the obsolete one-argument callback contract before any apply work."""

    try:
        # __wrapped__ describes the decorated function, not necessarily the
        # actual outer callable. Check the interface we will really invoke.
        _effective_indexer_signature(index).bind(
            object(), coordination=DocumentIndexCoordination.LOCKS_HELD
        )
    except (TypeError, ValueError):
        raise TypeError(
            "document_indexer must accept document and keyword-only coordination"
        ) from None

    # Inspect execution mode separately from the outer calling interface above.
    # A synchronous forwarding wrapper can still return its wrapped coroutine.
    # Follow known wrapper/partial/method edges conservatively, without invoking
    # the callback. Callable objects expose their execution mode via __call__.
    pending = [index]
    seen = set()
    while pending:
        candidate = pending.pop()
        if id(candidate) in seen:
            continue
        seen.add(id(candidate))
        if (
            inspect.iscoroutinefunction(candidate)
            or inspect.isgeneratorfunction(candidate)
            or inspect.isasyncgenfunction(candidate)
        ):
            raise DocumentIndexerContractError("document_indexer must be synchronous")
        wrapped = getattr(candidate, "__wrapped__", None)
        if wrapped is not None:
            pending.append(wrapped)
        if isinstance(candidate, partial):
            pending.append(candidate.func)
        if inspect.ismethod(candidate):
            pending.append(candidate.__func__)
        elif not inspect.isroutine(candidate):
            call = getattr(type(candidate), "__call__", None)  # noqa: B004 - inspect execution mode
            if call is not None:
                pending.append(call)


def validate_document_index_result(result: object) -> None:
    """Reject deferred/unknown results without running foreign callback logic.

    The standard indexer returns a nonnegative chunk count; synchronous adapters
    may discard it and return None. bool is not a chunk count. Only native,
    never-started deferred objects can be safely closed: closing a suspended
    object could execute its finally blocks. Never drive an arbitrary awaitable,
    call an unknown object's close method, or create an event loop here.
    """
    if result is None or (type(result) is int and result >= 0):
        return
    unstarted_native = (
        type(result) is CoroutineType
        and inspect.getcoroutinestate(result) == inspect.CORO_CREATED
        or type(result) is GeneratorType
        and inspect.getgeneratorstate(result) == inspect.GEN_CREATED
    )
    if unstarted_native:
        result.close()
    elif type(result) is AsyncGeneratorType:
        # Python 3.11 lacks safe async-generator state inspection. In that case
        # leave lifecycle ownership with the caller rather than guessing from
        # interpreter-specific frame offsets and potentially entering user code.
        get_state = getattr(inspect, "getasyncgenstate", None)
        if get_state is not None and get_state(result) == "AGEN_CREATED":
            # This native aclose operation on a never-started async generator
            # cannot enter its body/finally or await user code. No event loop.
            closing = result.aclose()
            try:
                closing.send(None)
            except StopIteration:
                pass
    raise DocumentIndexerContractError(
        "document_indexer must return None or a non-negative int after synchronous completion"
    )


def index_document(
    settings: Settings,
    document: SourceDocument,
    *,
    coordination: DocumentIndexCoordination,
    embedder=None,
    connection_factory=connect,
) -> int:
    """Atomically replace one document's chunks; safe to repeat.

    ``LOCKS_HELD`` is reserved for callers already holding the shared global
    and canonical document locks before they read the source snapshot. Making
    this state mandatory prevents an injected standard indexer from silently
    reacquiring its own lock on a second connection.

    In ``ACQUIRE_LOCKS`` mode the supplied document identifies the target only:
    its potentially stale content/metadata is discarded. Read the canonical
    source under the locks and hold them through embedding and index commit.
    """
    if coordination is not DocumentIndexCoordination.ACQUIRE_LOCKS and (
        coordination is not DocumentIndexCoordination.LOCKS_HELD
    ):
        raise ValueError("Invalid document-index coordination state")
    with connection_factory(settings) as conn, conn.transaction():
        if coordination is DocumentIndexCoordination.ACQUIRE_LOCKS:
            lock_document_index(conn, document.source_id, document.source_path)
            path, current = GitMarkdownSource(settings.knowledge_root).snapshot(
                document.source_id, document.source_path
            )
            if current is None:
                raise FileNotFoundError("Document index source is absent")
            document = SourceDocument(document.source_id, path, current, {"path": path})
        chunks = chunk_markdown_text(document.content, document.source_path)
        if embedder is None and chunks:
            from .embedder import LocalEmbedder

            embedder = LocalEmbedder(settings.embedding_model, settings.embedding_dimensions)
        vectors = embedder.encode([chunk.content for chunk in chunks]) if chunks else []
        if len(vectors) != len(chunks):
            raise RuntimeError("Embedding result count mismatch")
        # Delete and insert share one PostgreSQL transaction: readers observe the
        # complete old document or the complete new document, never a mixture.
        conn.execute(
            "DELETE FROM chunks WHERE source_id=%s AND source_path=%s",
            (document.source_id, document.source_path),
        )
        for chunk, vector in zip(chunks, vectors, strict=True):
            conn.execute(
                """INSERT INTO chunks
                (chunk_key, source_id, source_path, heading_path, ordinal, content,
                 content_hash, embedding_model, embedding, indexed_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,now())""",
                (
                    chunk.chunk_key,
                    document.source_id,
                    chunk.source_path,
                    chunk.heading_path,
                    chunk.ordinal,
                    chunk.content,
                    chunk.content_hash,
                    settings.embedding_model,
                    Vector(vector),
                ),
            )
        conn.execute(
            """INSERT INTO index_metadata (key, value)
            VALUES ('embedding_model', %s)
            ON CONFLICT (key) DO UPDATE
            SET value=EXCLUDED.value, updated_at=now()""",
            (settings.embedding_model,),
        )
    return len(chunks)


def _load_existing_chunk_state(
    conn,
    source_id: str,
) -> dict[str, ExistingChunkState]:
    rows = conn.execute(
        """
        SELECT chunk_key, content_hash, embedding_model
        FROM chunks
        WHERE source_id = %s
        """,
        (source_id,),
    ).fetchall()
    return {row[0]: (row[1], row[2]) for row in rows}


def index_knowledge(settings: Settings, source: SourceAdapter | None = None) -> None:
    source = source or GitMarkdownSource(settings.knowledge_root)
    with connect(settings) as conn, conn.transaction():
        # The exclusive global lock is acquired before filesystem discovery,
        # chunk calculation or delta reads. Applies/document indexing use the
        # shared form first, so a full run represents one coherent snapshot.
        lock_full_index(conn)
        documents = source.discover()
        print(f"[index] Found {len(documents)} document(s) from source {source.source_id}")

        all_chunks: list[Chunk] = []
        for document in documents:
            file_chunks = chunk_markdown_text(document.content, document.source_path)
            print(f"[index] {document.source_path} -> {len(file_chunks)} chunks")
            all_chunks.extend(file_chunks)

        existing_chunks = _load_existing_chunk_state(conn, source.source_id)
        current_keys = {chunk.chunk_key for chunk in all_chunks}
        changed = [
            chunk
            for chunk in all_chunks
            if existing_chunks.get(chunk.chunk_key)
            != (chunk.content_hash, settings.embedding_model)
        ]
        print(f"[index] {len(changed)} chunks need embeddings")

        embeddings_by_key: dict[str, Vector] = {}
        if changed:
            from .embedder import LocalEmbedder

            embedder = LocalEmbedder(settings.embedding_model, settings.embedding_dimensions)
            vectors = embedder.encode([chunk.content for chunk in changed])
            embeddings_by_key = {
                chunk.chunk_key: Vector(vector)
                for chunk, vector in zip(changed, vectors, strict=True)
            }

        for chunk in changed:
            conn.execute(
                """
                INSERT INTO chunks (
                    chunk_key,
                    source_id,
                    source_path,
                    heading_path,
                    ordinal,
                    content,
                    content_hash,
                    embedding_model,
                    embedding,
                    indexed_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (chunk_key) DO UPDATE SET
                    source_id = EXCLUDED.source_id,
                    source_path = EXCLUDED.source_path,
                    heading_path = EXCLUDED.heading_path,
                    ordinal = EXCLUDED.ordinal,
                    content = EXCLUDED.content,
                    content_hash = EXCLUDED.content_hash,
                    embedding_model = EXCLUDED.embedding_model,
                    embedding = EXCLUDED.embedding,
                    indexed_at = now()
                """,
                (
                    chunk.chunk_key,
                    source.source_id,
                    chunk.source_path,
                    chunk.heading_path,
                    chunk.ordinal,
                    chunk.content,
                    chunk.content_hash,
                    settings.embedding_model,
                    embeddings_by_key[chunk.chunk_key],
                ),
            )

        if current_keys:
            conn.execute(
                "DELETE FROM chunks WHERE source_id = %s AND NOT (chunk_key = ANY(%s))",
                (source.source_id, list(current_keys)),
            )
        else:
            conn.execute("DELETE FROM chunks WHERE source_id = %s", (source.source_id,))

        conn.execute(
            """
            INSERT INTO index_metadata (key, value)
            VALUES ('embedding_model', %s)
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value, updated_at = now()
            """,
            (settings.embedding_model,),
        )

    print(
        f"[index] Complete: {len(all_chunks)} current chunks, {len(changed)} newly embedded/updated"
    )
