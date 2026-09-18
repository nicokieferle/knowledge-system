from __future__ import annotations

from pgvector import Vector

from .chunking import Chunk, chunk_markdown_text
from .config import Settings
from .db import connect
from .sources import GitMarkdownSource, SourceAdapter, SourceDocument

ExistingChunkState = tuple[str, str]


def index_document(
    settings: Settings,
    document: SourceDocument,
    *,
    embedder=None,
    connection_factory=connect,
) -> int:
    """Atomically replace one applied document's chunks; safe to repeat."""
    chunks = chunk_markdown_text(document.content, document.source_path)
    if embedder is None and chunks:
        from .embedder import LocalEmbedder

        embedder = LocalEmbedder(settings.embedding_model, settings.embedding_dimensions)
    vectors = embedder.encode([chunk.content for chunk in chunks]) if chunks else []
    if len(vectors) != len(chunks):
        raise RuntimeError("Embedding result count mismatch")
    with connection_factory(settings) as conn, conn.transaction():
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
    settings: Settings,
    source_id: str,
) -> dict[str, ExistingChunkState]:
    with connect(settings) as conn:
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
    documents = source.discover()
    print(f"[index] Found {len(documents)} document(s) from source {source.source_id}")

    all_chunks: list[Chunk] = []

    for document in documents:
        file_chunks = chunk_markdown_text(document.content, document.source_path)
        print(f"[index] {document.source_path} -> {len(file_chunks)} chunks")
        all_chunks.extend(file_chunks)

    existing_chunks = _load_existing_chunk_state(settings, source.source_id)
    current_keys = {chunk.chunk_key for chunk in all_chunks}

    changed = [
        chunk
        for chunk in all_chunks
        if existing_chunks.get(chunk.chunk_key) != (chunk.content_hash, settings.embedding_model)
    ]

    print(f"[index] {len(changed)} chunks need embeddings")

    embeddings_by_key: dict[str, Vector] = {}

    if changed:
        from .embedder import LocalEmbedder

        embedder = LocalEmbedder(settings.embedding_model, settings.embedding_dimensions)
        vectors = embedder.encode([chunk.content for chunk in changed])
        embeddings_by_key = {
            chunk.chunk_key: Vector(vector) for chunk, vector in zip(changed, vectors, strict=True)
        }

    with connect(settings) as conn, conn.transaction():
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
