from __future__ import annotations

from typing import Protocol


class SchemaConnection(Protocol):
    def execute(self, query: str, params: object = None) -> object: ...


def init_conversation_schema(conn: SchemaConnection) -> None:
    """Create durable conversation and proposal tables without touching index data."""
    statements = (
        """
        CREATE TABLE IF NOT EXISTS conversations (
            id uuid PRIMARY KEY,
            title text,
            client_type text NOT NULL,
            external_conversation_id text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            archived_at timestamptz
        )
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS conversations_client_external_id_idx
        ON conversations (client_type, external_conversation_id)
        WHERE external_conversation_id IS NOT NULL
        """,
        """
        CREATE TABLE IF NOT EXISTS client_states (
            client_type text NOT NULL,
            external_chat_id text NOT NULL,
            external_user_id text NOT NULL,
            active_conversation_id uuid REFERENCES conversations(id),
            version bigint NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (client_type, external_chat_id, external_user_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS client_conversations (
            client_type text NOT NULL,
            external_chat_id text NOT NULL,
            external_user_id text NOT NULL,
            conversation_id uuid NOT NULL REFERENCES conversations(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (client_type, external_chat_id, external_user_id, conversation_id),
            FOREIGN KEY (client_type, external_chat_id, external_user_id)
                REFERENCES client_states(client_type, external_chat_id, external_user_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS client_message_bindings (
            client_type text NOT NULL,
            external_chat_id text NOT NULL,
            external_user_id text NOT NULL,
            external_message_id text NOT NULL,
            conversation_id uuid NOT NULL REFERENCES conversations(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (client_type, external_chat_id, external_user_id, external_message_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS messages (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            conversation_id uuid NOT NULL REFERENCES conversations(id),
            role text NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
            content text NOT NULL,
            external_message_id text,
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (conversation_id, id)
        )
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS messages_conversation_external_id_idx
        ON messages (conversation_id, external_message_id)
        WHERE external_message_id IS NOT NULL
        """,
        """
        CREATE INDEX IF NOT EXISTS messages_conversation_order_idx
        ON messages (conversation_id, id)
        """,
        """
        CREATE TABLE IF NOT EXISTS conversation_summaries (
            id uuid PRIMARY KEY,
            conversation_id uuid NOT NULL UNIQUE REFERENCES conversations(id),
            summary text NOT NULL,
            through_message_id bigint NOT NULL,
            version integer NOT NULL DEFAULT 1 CHECK (version > 0),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            FOREIGN KEY (conversation_id, through_message_id)
                REFERENCES messages(conversation_id, id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS proposal_suggestions (
            id uuid PRIMARY KEY,
            conversation_id uuid NOT NULL REFERENCES conversations(id),
            trigger_message_id bigint NOT NULL,
            originating_message_ids bigint[] NOT NULL DEFAULT '{}',
            context_summary text,
            status text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'confirmed', 'rejected')),
            resolution_message_id bigint,
            created_at timestamptz NOT NULL DEFAULT now(),
            resolved_at timestamptz,
            UNIQUE (conversation_id, trigger_message_id),
            FOREIGN KEY (conversation_id, trigger_message_id)
                REFERENCES messages(conversation_id, id),
            FOREIGN KEY (conversation_id, resolution_message_id)
                REFERENCES messages(conversation_id, id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS proposals (
            id uuid PRIMARY KEY,
            conversation_id uuid NOT NULL REFERENCES conversations(id),
            status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending')),
            source_client text NOT NULL,
            trigger_type text NOT NULL CHECK (
                trigger_type IN ('command', 'intent', 'confirmed_suggestion')
            ),
            trigger_message_id bigint NOT NULL,
            originating_message_ids bigint[] NOT NULL DEFAULT '{}',
            target_source_id text,
            target_source_path text,
            title text,
            summary text NOT NULL,
            reason text NOT NULL,
            proposed_content text NOT NULL,
            base_revision text,
            source_suggestion_id uuid UNIQUE REFERENCES proposal_suggestions(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (conversation_id, trigger_message_id, trigger_type),
            FOREIGN KEY (conversation_id, trigger_message_id)
                REFERENCES messages(conversation_id, id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS proposal_suggestions_conversation_status_idx
        ON proposal_suggestions (conversation_id, status)
        """,
        """
        CREATE INDEX IF NOT EXISTS proposals_conversation_created_idx
        ON proposals (conversation_id, created_at)
        """,
    )

    for statement in statements:
        conn.execute(statement)

    from .review_schema import REVIEW_IMMUTABILITY, REVIEW_MIGRATION

    conn.execute(REVIEW_MIGRATION)
    conn.execute(REVIEW_IMMUTABILITY)
