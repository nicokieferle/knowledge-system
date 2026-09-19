"""PostgreSQL advisory-lock protocol for filesystem and index consistency."""

from __future__ import annotations

import hashlib

from .sources import portable_source_lock_components

# PostgreSQL's one-bigint advisory-lock space is split explicitly: the global
# index lock is in the 01 domain and document locks are in the 10 domain. The
# remaining 62 document bits are a stable BLAKE2 digest. Collisions therefore
# remain theoretically possible, but are no longer the practical 32-bit
# hashtext collisions of the previous protocol and can never hit the global key.
GLOBAL_INDEX_LOCK_KEY = 0x4000000000000001
_DOCUMENT_LOCK_DOMAIN = 0x8000000000000000
_DOCUMENT_LOCK_PAYLOAD_MASK = (1 << 62) - 1
_UINT64_MODULUS = 1 << 64


def document_lock_key(source_id: str, source_path: str) -> int:
    """Return a stable signed-bigint lock key for one canonical document."""

    canonical_source, canonical_path = portable_source_lock_components(source_id, source_path)
    source = canonical_source.encode("ascii")
    path = canonical_path.encode("ascii")
    # Length framing keeps source/path boundaries unambiguous if the validated
    # namespace ever grows beyond today's fixed source and ASCII path alphabet.
    framed = len(source).to_bytes(2, "big") + source + len(path).to_bytes(2, "big") + path
    digest = hashlib.blake2b(
        framed,
        digest_size=8,
        person=b"ks-doc-lock-v1",
    ).digest()
    unsigned = _DOCUMENT_LOCK_DOMAIN | (int.from_bytes(digest, "big") & _DOCUMENT_LOCK_PAYLOAD_MASK)
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
