from __future__ import annotations

from dataclasses import dataclass

from pgvector import Vector

from .config import Settings
from .db import connect


@dataclass(frozen=True)
class SearchResult:
    source_path: str
    heading_path: str
    content: str
    similarity: float


def semantic_search(settings: Settings, query: str, limit: int = 5) -> list[SearchResult]:
    from .embedder import LocalEmbedder

    embedder = LocalEmbedder(settings.embedding_model, settings.embedding_dimensions)
    query_vector = Vector(embedder.encode([query])[0])

    with connect(settings) as conn:
        rows = conn.execute(
            """
            SELECT
                source_path,
                heading_path,
                content,
                1 - (embedding <=> %s) AS similarity
            FROM chunks
            WHERE embedding_model = %s
            ORDER BY embedding <=> %s
            LIMIT %s
            """,
            (query_vector, settings.embedding_model, query_vector, limit),
        ).fetchall()

    return [
        SearchResult(
            source_path=row[0],
            heading_path=row[1],
            content=row[2],
            similarity=float(row[3]),
        )
        for row in rows
    ]
