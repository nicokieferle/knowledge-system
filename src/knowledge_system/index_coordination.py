"""PostgreSQL advisory-lock protocol for filesystem and index consistency."""

from __future__ import annotations

from .sources import portable_source_lock_identity

INDEX_LOCK_NAMESPACE = 330033
DOCUMENT_LOCK_NAMESPACE = 330034


def lock_full_index(conn) -> None:
    """Exclude applies and document indexing before taking a full snapshot."""

    conn.execute(
        "SELECT pg_advisory_xact_lock(%s, %s)",
        (INDEX_LOCK_NAMESPACE, 0),
    )


def lock_document_index(conn, source_id: str, source_path: str) -> None:
    """Take shared global, then canonical document lock in stable order."""

    identity = portable_source_lock_identity(source_id, source_path)
    conn.execute(
        "SELECT pg_advisory_xact_lock_shared(%s, %s)",
        (INDEX_LOCK_NAMESPACE, 0),
    )
    conn.execute(
        "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
        (DOCUMENT_LOCK_NAMESPACE, identity),
    )
