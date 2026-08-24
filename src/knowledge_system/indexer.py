from __future__ import annotations

from pathlib import Path

from pgvector import Vector

from .chunking import Chunk, chunk_markdown
from .config import Settings
from .db import connect

ExistingChunkState = tuple[str, str]
ROOT_MARKDOWN_EXCLUDES = {"README.md"}


def discover_markdown(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*.md")
        if path.is_file()
        and path.relative_to(root).as_posix() not in ROOT_MARKDOWN_EXCLUDES
        and not any(part.startswith(".") for part in path.relative_to(root).parts)
    )


def _load_existing_chunk_state(settings: Settings) -> dict[str, ExistingChunkState]:
    with connect(settings) as conn:
        rows = conn.execute("SELECT chunk_key, content_hash, embedding_model FROM chunks").fetchall()
    return {row[0]: (row[1], row[2]) for row in rows}


def index_knowledge(settings: Settings) -> None:
    root = settings.knowledge_root
    if not root.exists():
        raise FileNotFoundError(f"Knowledge root does not exist: {root}")

    files = discover_markdown(root)
    print(f"[index] Found {len(files)} Markdown files under {root}")

    all_chunks: list[Chunk] = []

    for path in files:
        file_chunks = chunk_markdown(path, root)
        print(f"[index] {path.relative_to(root)} -> {len(file_chunks)} chunks")
        all_chunks.extend(file_chunks)

    existing_chunks = _load_existing_chunk_state(settings)
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
            chunk.chunk_key: Vector(vector)
            for chunk, vector in zip(changed, vectors, strict=True)
        }

    with connect(settings) as conn, conn.transaction():
        for chunk in changed:
            conn.execute(
                """
                INSERT INTO chunks (
                    chunk_key,
                    source_path,
                    heading_path,
                    ordinal,
                    content,
                    content_hash,
                    embedding_model,
                    embedding,
                    indexed_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (chunk_key) DO UPDATE SET
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
                "DELETE FROM chunks WHERE NOT (chunk_key = ANY(%s))",
                (list(current_keys),),
            )
        else:
            conn.execute("DELETE FROM chunks")

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
        f"[index] Complete: {len(all_chunks)} current chunks, "
        f"{len(changed)} newly embedded/updated"
    )
