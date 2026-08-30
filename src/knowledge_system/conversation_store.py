from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from typing import Any, Protocol
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from .config import Settings
from .conversation_models import Conversation, ConversationSummary, Message, MessageRole
from .db import connect


class ConversationNotFoundError(LookupError):
    pass


class MessageNotFoundError(LookupError):
    pass


class ConversationStore(Protocol):
    def create_conversation(
        self,
        client_type: str,
        title: str | None = None,
        external_conversation_id: str | None = None,
    ) -> Conversation: ...

    def get_conversation(self, conversation_id: UUID) -> Conversation: ...

    def add_message(
        self,
        conversation_id: UUID,
        role: MessageRole,
        content: str,
        external_message_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> Message: ...

    def get_message(self, conversation_id: UUID, message_id: int) -> Message: ...

    def list_messages(
        self,
        conversation_id: UUID,
        *,
        after_message_id: int | None = None,
        before_message_id: int | None = None,
    ) -> list[Message]: ...

    def get_messages(self, conversation_id: UUID, message_ids: Sequence[int]) -> list[Message]: ...

    def get_summary(self, conversation_id: UUID) -> ConversationSummary | None: ...

    def compare_and_set_summary(
        self,
        conversation_id: UUID,
        *,
        expected_version: int,
        summary: str,
        through_message_id: int,
    ) -> bool: ...


ConnectionFactory = Callable[[Settings], AbstractContextManager[Any]]


class PostgresConversationStore:
    def __init__(
        self,
        settings: Settings,
        connection_factory: ConnectionFactory = connect,
    ) -> None:
        self.settings = settings
        self._connect = connection_factory

    def create_conversation(
        self,
        client_type: str,
        title: str | None = None,
        external_conversation_id: str | None = None,
    ) -> Conversation:
        client_type = client_type.strip()
        if not client_type:
            raise ValueError("client_type must not be empty")

        conversation_id = uuid4()
        with self._connect(self.settings) as conn, conn.transaction():
            row = conn.execute(
                """
                INSERT INTO conversations (id, title, client_type, external_conversation_id)
                VALUES (%s, %s, %s, %s)
                RETURNING id, client_type, title, external_conversation_id,
                          created_at, updated_at, archived_at
                """,
                (conversation_id, title, client_type, external_conversation_id),
            ).fetchone()
        print(f"[conversation] Created conversation {conversation_id} client={client_type}")
        return _conversation_from_row(row)

    def get_conversation(self, conversation_id: UUID) -> Conversation:
        with self._connect(self.settings) as conn:
            row = conn.execute(
                """
                SELECT id, client_type, title, external_conversation_id,
                       created_at, updated_at, archived_at
                FROM conversations
                WHERE id = %s
                """,
                (conversation_id,),
            ).fetchone()
        if row is None:
            raise ConversationNotFoundError(f"Unknown conversation `{conversation_id}`")
        return _conversation_from_row(row)

    def add_message(
        self,
        conversation_id: UUID,
        role: MessageRole,
        content: str,
        external_message_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> Message:
        if not content.strip():
            raise ValueError("message content must not be empty")

        with self._connect(self.settings) as conn, conn.transaction():
            row = conn.execute(
                """
                INSERT INTO messages (
                    conversation_id, role, content, external_message_id, metadata
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (conversation_id, external_message_id)
                    WHERE external_message_id IS NOT NULL
                DO UPDATE SET external_message_id = EXCLUDED.external_message_id
                RETURNING id, conversation_id, role, content, created_at,
                          external_message_id, metadata
                """,
                (
                    conversation_id,
                    role.value,
                    content,
                    external_message_id,
                    Jsonb(metadata or {}),
                ),
            ).fetchone()
            conn.execute(
                "UPDATE conversations SET updated_at = now() WHERE id = %s",
                (conversation_id,),
            )
        return _message_from_row(row)

    def get_message(self, conversation_id: UUID, message_id: int) -> Message:
        with self._connect(self.settings) as conn:
            row = conn.execute(
                """
                SELECT id, conversation_id, role, content, created_at,
                       external_message_id, metadata
                FROM messages
                WHERE conversation_id = %s AND id = %s
                """,
                (conversation_id, message_id),
            ).fetchone()
        if row is None:
            raise MessageNotFoundError(
                f"Unknown message `{message_id}` in conversation `{conversation_id}`"
            )
        return _message_from_row(row)

    def list_messages(
        self,
        conversation_id: UUID,
        *,
        after_message_id: int | None = None,
        before_message_id: int | None = None,
    ) -> list[Message]:
        clauses = ["conversation_id = %s"]
        params: list[object] = [conversation_id]
        if after_message_id is not None:
            clauses.append("id > %s")
            params.append(after_message_id)
        if before_message_id is not None:
            clauses.append("id < %s")
            params.append(before_message_id)

        query = f"""
            SELECT id, conversation_id, role, content, created_at,
                   external_message_id, metadata
            FROM messages
            WHERE {' AND '.join(clauses)}
            ORDER BY id
        """
        with self._connect(self.settings) as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [_message_from_row(row) for row in rows]

    def get_messages(self, conversation_id: UUID, message_ids: Sequence[int]) -> list[Message]:
        if not message_ids:
            return []
        with self._connect(self.settings) as conn:
            rows = conn.execute(
                """
                SELECT id, conversation_id, role, content, created_at,
                       external_message_id, metadata
                FROM messages
                WHERE conversation_id = %s AND id = ANY(%s)
                ORDER BY id
                """,
                (conversation_id, list(message_ids)),
            ).fetchall()
        messages = [_message_from_row(row) for row in rows]
        if len(messages) != len(set(message_ids)):
            raise MessageNotFoundError("One or more originating messages do not exist")
        return messages

    def get_summary(self, conversation_id: UUID) -> ConversationSummary | None:
        with self._connect(self.settings) as conn:
            row = conn.execute(
                """
                SELECT id, conversation_id, summary, through_message_id, version,
                       created_at, updated_at
                FROM conversation_summaries
                WHERE conversation_id = %s
                """,
                (conversation_id,),
            ).fetchone()
        return None if row is None else _summary_from_row(row)

    def compare_and_set_summary(
        self,
        conversation_id: UUID,
        *,
        expected_version: int,
        summary: str,
        through_message_id: int,
    ) -> bool:
        with self._connect(self.settings) as conn, conn.transaction():
            if expected_version == 0:
                row = conn.execute(
                    """
                    INSERT INTO conversation_summaries (
                        id, conversation_id, summary, through_message_id, version
                    )
                    VALUES (%s, %s, %s, %s, 1)
                    ON CONFLICT (conversation_id) DO NOTHING
                    RETURNING version
                    """,
                    (uuid4(), conversation_id, summary, through_message_id),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    UPDATE conversation_summaries
                    SET summary = %s,
                        through_message_id = %s,
                        version = version + 1,
                        updated_at = now()
                    WHERE conversation_id = %s
                      AND version = %s
                      AND through_message_id < %s
                    RETURNING version
                    """,
                    (
                        summary,
                        through_message_id,
                        conversation_id,
                        expected_version,
                        through_message_id,
                    ),
                ).fetchone()
        return row is not None


def _conversation_from_row(row: Sequence[Any]) -> Conversation:
    return Conversation(
        id=row[0],
        client_type=row[1],
        title=row[2],
        external_conversation_id=row[3],
        created_at=row[4],
        updated_at=row[5],
        archived_at=row[6],
    )


def _message_from_row(row: Sequence[Any]) -> Message:
    return Message(
        id=row[0],
        conversation_id=row[1],
        role=MessageRole(row[2]),
        content=row[3],
        created_at=row[4],
        external_message_id=row[5],
        metadata=dict(row[6]),
    )


def _summary_from_row(row: Sequence[Any]) -> ConversationSummary:
    return ConversationSummary(
        id=row[0],
        conversation_id=row[1],
        summary=row[2],
        through_message_id=row[3],
        version=row[4],
        created_at=row[5],
        updated_at=row[6],
    )
