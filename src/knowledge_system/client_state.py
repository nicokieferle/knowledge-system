from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from .config import Settings
from .conversation_models import Conversation
from .conversation_store import ConnectionFactory
from .db import connect


@dataclass(frozen=True)
class ClientIdentity:
    client_type: str
    external_chat_id: str
    external_user_id: str


@dataclass(frozen=True)
class ClientState:
    identity: ClientIdentity
    active_conversation_id: UUID | None
    version: int


class ClientStateStore(Protocol):
    def get(self, identity: ClientIdentity) -> ClientState | None: ...

    def activate(self, identity: ClientIdentity, conversation_id: UUID) -> ClientState: ...

    def list_conversations(
        self, identity: ClientIdentity, limit: int = 20
    ) -> list[Conversation]: ...

    def bind_message(
        self, identity: ClientIdentity, external_message_id: str, conversation_id: UUID
    ) -> UUID: ...


class PostgresClientStateStore:
    """Durable, provider-neutral ownership and active-topic mapping."""

    def __init__(self, settings: Settings, connection_factory: ConnectionFactory = connect) -> None:
        self.settings = settings
        self._connect = connection_factory

    def get(self, identity: ClientIdentity) -> ClientState | None:
        with self._connect(self.settings) as conn:
            row = conn.execute(
                """SELECT active_conversation_id, version FROM client_states
                   WHERE client_type=%s AND external_chat_id=%s AND external_user_id=%s""",
                self._key(identity),
            ).fetchone()
        return None if row is None else ClientState(identity, row[0], row[1])

    def activate(self, identity: ClientIdentity, conversation_id: UUID) -> ClientState:
        with self._connect(self.settings) as conn, conn.transaction():
            # Serialize updates for one client identity; this prevents lost active-topic updates.
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (self._advisory_lock_key(identity),),
            )
            row = conn.execute(
                """
                INSERT INTO client_states (
                    client_type, external_chat_id, external_user_id, active_conversation_id
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (client_type, external_chat_id, external_user_id) DO UPDATE
                SET active_conversation_id=EXCLUDED.active_conversation_id,
                    version=client_states.version+1, updated_at=now()
                RETURNING active_conversation_id, version
                """,
                (*self._key(identity), conversation_id),
            ).fetchone()
            conn.execute(
                """INSERT INTO client_conversations
                   (client_type, external_chat_id, external_user_id, conversation_id)
                   VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                (*self._key(identity), conversation_id),
            )
        return ClientState(identity, row[0], row[1])

    def list_conversations(self, identity: ClientIdentity, limit: int = 20) -> list[Conversation]:
        with self._connect(self.settings) as conn:
            rows = conn.execute(
                """
                SELECT c.id, c.client_type, c.title, c.external_conversation_id,
                       c.created_at, c.updated_at, c.archived_at
                FROM client_conversations cc JOIN conversations c ON c.id=cc.conversation_id
                WHERE cc.client_type=%s AND cc.external_chat_id=%s AND cc.external_user_id=%s
                  AND c.archived_at IS NULL
                ORDER BY c.updated_at DESC LIMIT %s
                """,
                (*self._key(identity), limit),
            ).fetchall()
        return [self._conversation(row) for row in rows]

    def bind_message(
        self, identity: ClientIdentity, external_message_id: str, conversation_id: UUID
    ) -> UUID:
        """Return the first topic bound to an external message, including on retry."""
        with self._connect(self.settings) as conn, conn.transaction():
            row = conn.execute(
                """INSERT INTO client_message_bindings
                   (client_type, external_chat_id, external_user_id,
                    external_message_id, conversation_id)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (client_type, external_chat_id, external_user_id,
                                external_message_id)
                   DO UPDATE SET external_message_id=EXCLUDED.external_message_id
                   RETURNING conversation_id""",
                (*self._key(identity), external_message_id, conversation_id),
            ).fetchone()
        return row[0]

    @staticmethod
    def _key(identity: ClientIdentity) -> tuple[str, str, str]:
        values = (
            identity.client_type.strip(),
            identity.external_chat_id,
            identity.external_user_id,
        )
        if not all(values):
            raise ValueError("client identity values must not be empty")
        return values

    @classmethod
    def _advisory_lock_key(cls, identity: ClientIdentity) -> str:
        key = cls._key(identity)
        return json.dumps(
            {
                "client_type": key[0],
                "external_chat_id": key[1],
                "external_user_id": key[2],
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _conversation(row: Sequence[Any]) -> Conversation:
        return Conversation(row[0], row[1], row[2], row[3], row[4], row[5], row[6])
