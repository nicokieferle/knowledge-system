from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from pgvector.psycopg import register_vector

from .config import Settings


@contextmanager
def connect(settings: Settings, autocommit: bool = False) -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(settings.database_url, autocommit=autocommit)
    try:
        register_vector(conn)
        yield conn
    finally:
        conn.close()


def init_db(settings: Settings) -> None:
    print("[db] Initializing database")
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        register_vector(conn)

        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS chunks (
                chunk_key text PRIMARY KEY,
                source_id text NOT NULL DEFAULT 'knowledge-git',
                source_path text NOT NULL,
                heading_path text NOT NULL,
                ordinal integer NOT NULL,
                content text NOT NULL,
                content_hash text NOT NULL,
                embedding_model text NOT NULL,
                embedding vector({settings.embedding_dimensions}) NOT NULL,
                indexed_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )

        conn.execute(
            """
            ALTER TABLE chunks
            ADD COLUMN IF NOT EXISTS source_id text NOT NULL DEFAULT 'knowledge-git'
            """
        )
        conn.execute("ALTER TABLE chunks DROP CONSTRAINT IF EXISTS chunks_source_path_ordinal_key")

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS chunks_source_path_idx
            ON chunks (source_path)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS chunks_source_id_source_path_idx
            ON chunks (source_id, source_path)
            """
        )

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS chunks_source_id_source_path_ordinal_idx
            ON chunks (source_id, source_path, ordinal)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS chunks_fts_german_idx
            ON chunks
            USING GIN (to_tsvector('german'::regconfig, heading_path || ' ' || content))
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS chunks_fts_simple_idx
            ON chunks
            USING GIN (to_tsvector('simple'::regconfig, heading_path || ' ' || content))
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS index_metadata (
                key text PRIMARY KEY,
                value text NOT NULL,
                updated_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )

        conn.execute(
            """
            INSERT INTO index_metadata (key, value)
            VALUES ('schema_version', '1')
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value, updated_at = now()
            """
        )

    print("[db] Database ready")
