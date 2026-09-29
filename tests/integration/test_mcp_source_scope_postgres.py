"""Source scoping in the real PostgreSQL keyword query, with synthetic rows."""

from __future__ import annotations

import os
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from knowledge_system.config import Settings
from knowledge_system.search import keyword_search


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="isolated PostgreSQL required")
def test_keyword_search_source_scope_precedes_limit(tmp_path) -> None:
    address = os.environ["TEST_DATABASE_URL"]
    parsed = urlsplit(address)
    assert parsed.hostname in ("localhost", "127.0.0.1")
    assert parsed.path == "/knowledge_ci_test" and not parsed.query
    schema = "mcp_scope_" + uuid4().hex
    with psycopg.connect(address, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        scoped_address = address + "?" + urlencode({"options": f"-csearch_path={schema},public"})
        settings = Settings(scoped_address, tmp_path, "unused", 384)
        with psycopg.connect(scoped_address, autocommit=True) as conn:
            conn.execute(
                "CREATE TABLE chunks (chunk_key text, source_id text, source_path text, "
                "heading_path text, content text, ordinal integer)"
            )
            conn.execute(
                "INSERT INTO chunks VALUES "
                "('private', 'private-source', 'private.md', 'Synthetic', "
                "'synthetic synthetic synthetic synthetic', 1), "
                "('allowed', 'knowledge-git', 'allowed.md', 'Synthetic', 'synthetic', 1)"
            )
        unscoped = keyword_search(settings, "synthetic", limit=1, verbose=False)
        scoped = keyword_search(
            settings,
            "synthetic",
            limit=1,
            verbose=False,
            source_ids=frozenset({"knowledge-git"}),
        )
        assert unscoped[0].source_id == "private-source"
        assert [(result.source_id, result.source_path) for result in scoped] == [
            ("knowledge-git", "allowed.md")
        ]
    finally:
        with psycopg.connect(address, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
