from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from .config import Settings
from .db import connect

DURABLE_TABLES = (
    "conversations",
    "messages",
    "conversation_summaries",
    "proposal_suggestions",
    "proposals",
)


@dataclass(frozen=True)
class DurableStateFingerprint:
    counts: dict[str, int]
    sha256: str


def read_durable_state_fingerprint(settings: Settings) -> DurableStateFingerprint:
    """Hash durable rows without exposing private conversation or proposal text."""
    queries = {
        "conversations": """
            SELECT id, title, client_type, external_conversation_id,
                   created_at, updated_at, archived_at
            FROM conversations ORDER BY id
        """,
        "messages": """
            SELECT id, conversation_id, role, content, created_at,
                   external_message_id, metadata
            FROM messages ORDER BY id
        """,
        "conversation_summaries": """
            SELECT id, conversation_id, summary, through_message_id, version,
                   created_at, updated_at
            FROM conversation_summaries ORDER BY id
        """,
        "proposal_suggestions": """
            SELECT id, conversation_id, trigger_message_id, originating_message_ids,
                   context_summary, status, resolution_message_id, created_at, resolved_at
            FROM proposal_suggestions ORDER BY id
        """,
        "proposals": """
            SELECT id, conversation_id, status, source_client, trigger_type,
                   trigger_message_id, originating_message_ids, target_source_id,
                   target_source_path, title, summary, reason, proposed_content,
                   base_revision, source_suggestion_id, created_at
            FROM proposals ORDER BY id
        """,
    }
    sensitive_columns = {
        "conversations": {1, 3},
        "messages": {3, 5, 6},
        "conversation_summaries": {2},
        "proposal_suggestions": {4},
        "proposals": {7, 8, 9, 10, 11, 12, 13},
    }

    payload: dict[str, list[list[object]]] = {}
    with connect(settings) as conn:
        for table in DURABLE_TABLES:
            rows = conn.execute(queries[table]).fetchall()
            payload[table] = [
                [
                    _sensitive_digest(value) if index in sensitive_columns[table] else _stable(value)
                    for index, value in enumerate(row)
                ]
                for row in rows
            ]

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return DurableStateFingerprint(
        counts={table: len(payload[table]) for table in DURABLE_TABLES},
        sha256=hashlib.sha256(encoded).hexdigest(),
    )


def _sensitive_digest(value: object) -> str | None:
    if value is None:
        return None
    encoded = json.dumps(_stable(value), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _stable(value: Any) -> object:
    if isinstance(value, (UUID, datetime, date)):
        return value.isoformat() if not isinstance(value, UUID) else str(value)
    if isinstance(value, dict):
        return {str(key): _stable(item) for key, item in sorted(value.items())}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_stable(item) for item in value]
    return value
