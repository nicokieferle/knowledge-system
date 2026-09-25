"""PostgreSQL advisory-lock protocol for filesystem and index consistency."""

from __future__ import annotations

import hashlib

from .sources import portable_source_lock_components

# PostgreSQL's one-bigint advisory-lock space is split explicitly: the global
# index lock is in the 01 domain, document locks in 10 and parent-write locks in
# 110 (also disjoint from existing signed-32-bit client/schema keys). The remaining
# 62 document / 61 parent bits are a stable BLAKE2 digest. Collisions therefore
# remain theoretically possible, but are no longer the practical 32-bit
# hashtext collisions of the previous protocol and can never hit the global key.
GLOBAL_INDEX_LOCK_KEY = 0x4000000000000001
_DOCUMENT_LOCK_DOMAIN = 0x8000000000000000
_PARENT_LOCK_DOMAIN = 0xC000000000000000
_DOCUMENT_LOCK_PAYLOAD_MASK = (1 << 62) - 1
_PARENT_LOCK_PAYLOAD_MASK = (1 << 61) - 1
_UINT64_MODULUS = 1 << 64


def document_lock_key(source_id: str, source_path: str) -> int:
    """Return a stable signed-bigint lock key for one canonical document."""

    canonical_source, canonical_path = portable_source_lock_components(source_id, source_path)
    return _scoped_lock_key(
        canonical_source,
        canonical_path,
        _DOCUMENT_LOCK_DOMAIN,
        _DOCUMENT_LOCK_PAYLOAD_MASK,
        b"ks-doc-lock-v1",
    )


def parent_write_lock_key(source_id: str, source_path: str) -> int:
    """Coordinate directory mutations without serializing other parent directories."""
    source, path = portable_source_lock_components(source_id, source_path)
    parent = path.rpartition("/")[0]
    return _scoped_lock_key(
        source, parent, _PARENT_LOCK_DOMAIN, _PARENT_LOCK_PAYLOAD_MASK, b"ks-dir-lock-v1"
    )


def _scoped_lock_key(
    source_id: str, source_path: str, domain: int, payload_mask: int, person: bytes
) -> int:
    source = source_id.encode("ascii")
    path = source_path.encode("ascii")
    # Length framing keeps source/path boundaries unambiguous if the validated
    # namespace ever grows beyond today's fixed source and ASCII path alphabet.
    framed = len(source).to_bytes(2, "big") + source + len(path).to_bytes(2, "big") + path
    digest = hashlib.blake2b(
        framed,
        digest_size=8,
        person=person,
    ).digest()
    unsigned = domain | (int.from_bytes(digest, "big") & payload_mask)
    return unsigned - _UINT64_MODULUS


def lock_full_index(conn) -> None:
    """Exclude applies and document indexing before taking a full snapshot."""

    conn.execute(
        "SELECT pg_advisory_xact_lock(%s)",
        (GLOBAL_INDEX_LOCK_KEY,),
    )


def lock_document_index(conn, source_id: str, source_path: str) -> None:
    """Take shared global, then canonical document lock in stable order."""

    key = document_lock_key(source_id, source_path)
    conn.execute(
        "SELECT pg_advisory_xact_lock_shared(%s)",
        (GLOBAL_INDEX_LOCK_KEY,),
    )
    conn.execute(
        "SELECT pg_advisory_xact_lock(%s)",
        (key,),
    )


def lock_parent_write(conn, source_id: str, source_path: str) -> None:
    """After the document lock, pin the parent's namespace for the apply transaction.

    Only filesystem writers need this lock. Shared-parent staging/rename/unlink
    would otherwise invalidate each other's conservative directory metadata guard.
    It is released before document indexing; different parents remain independent.
    """
    conn.execute(
        "SELECT pg_advisory_xact_lock(%s)",
        (parent_write_lock_key(source_id, source_path),),
    )
