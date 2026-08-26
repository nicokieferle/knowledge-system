from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pgvector import Vector

from .config import Settings
from .db import connect


@dataclass(frozen=True)
class SearchResult:
    source_path: str
    heading_path: str
    content: str
    similarity: float


class Embedder(Protocol):
    def encode(self, texts: list[str]) -> object: ...


TEXT_SEARCH_CONFIGS = {"german", "simple"}


def _validate_text_search_config(text_config: str) -> str:
    if text_config not in TEXT_SEARCH_CONFIGS:
        allowed = ", ".join(sorted(TEXT_SEARCH_CONFIGS))
        raise ValueError(f"Unsupported text search config `{text_config}`. Allowed: {allowed}")
    return text_config


def semantic_search_with_embedder(
    settings: Settings,
    query: str,
    embedder: Embedder,
    limit: int = 5,
    verbose: bool = True,
) -> list[SearchResult]:
    query_vector = Vector(embedder.encode([query])[0])
    if verbose:
        print("[search] Query embedding ready")

    with connect(settings) as conn:
        if verbose:
            print(f"[search] Querying vector index: limit={limit}")
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

    if verbose:
        print(f"[search] Retrieved {len(rows)} result(s)")
    return [
        SearchResult(
            source_path=row[0],
            heading_path=row[1],
            content=row[2],
            similarity=float(row[3]),
        )
        for row in rows
    ]


def semantic_search(
    settings: Settings,
    query: str,
    limit: int = 5,
    verbose: bool = True,
) -> list[SearchResult]:
    from .embedder import LocalEmbedder

    embedder = LocalEmbedder(settings.embedding_model, settings.embedding_dimensions, verbose=verbose)
    return semantic_search_with_embedder(settings, query, embedder, limit=limit, verbose=verbose)


def keyword_search(
    settings: Settings,
    query: str,
    limit: int = 5,
    text_config: str = "german",
    verbose: bool = True,
) -> list[SearchResult]:
    text_config = _validate_text_search_config(text_config)
    query = query.strip()
    if not query:
        if verbose:
            print("[search] Empty keyword query")
        return []

    if verbose:
        print(f"[search] Querying keyword index: config={text_config} limit={limit}")

    with connect(settings) as conn:
        rows = conn.execute(
            """
            WITH query_terms AS (
                SELECT DISTINCT lexeme
                FROM ts_debug(%s::regconfig, %s), unnest(lexemes) AS lexeme
                WHERE lexeme <> ''
            ),
            query AS (
                SELECT string_agg(quote_literal(lexeme), ' | ')::tsquery AS tsquery
                FROM query_terms
            )
            SELECT
                source_path,
                heading_path,
                content,
                ts_rank_cd(
                    to_tsvector(%s::regconfig, heading_path || ' ' || content),
                    query.tsquery
                ) AS rank
            FROM chunks, query
            WHERE query.tsquery IS NOT NULL
                AND query.tsquery @@ to_tsvector(%s::regconfig, heading_path || ' ' || content)
            ORDER BY rank DESC, source_path, ordinal
            LIMIT %s
            """,
            (text_config, query, text_config, text_config, limit),
        ).fetchall()

    if verbose:
        print(f"[search] Retrieved {len(rows)} keyword result(s)")
    return [
        SearchResult(
            source_path=row[0],
            heading_path=row[1],
            content=row[2],
            similarity=float(row[3]),
        )
        for row in rows
    ]
