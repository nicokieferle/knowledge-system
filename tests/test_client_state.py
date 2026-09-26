from __future__ import annotations

from contextlib import contextmanager
from uuid import uuid4

from knowledge_system.client_state import ClientIdentity, PostgresClientStateStore
from knowledge_system.config import Settings


class Cursor:
    def __init__(self, row=None) -> None:
        self.row = row

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, conversation_id) -> None:
        self.conversation_id = conversation_id
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, query: str, params: tuple[object, ...] = ()) -> Cursor:
        self.calls.append((query, params))
        if "RETURNING active_conversation_id, version" in query:
            return Cursor((self.conversation_id, 1))
        return Cursor()

    @contextmanager
    def transaction(self):
        yield


def test_advisory_lock_key_is_stable_for_same_identity() -> None:
    identity = ClientIdentity("telegram", "chat-1", "user-1")

    assert PostgresClientStateStore._advisory_lock_key(
        identity
    ) == PostgresClientStateStore._advisory_lock_key(identity)


def test_advisory_lock_key_distinguishes_ambiguous_tuple_concatenations() -> None:
    first = ClientIdentity("ab", "c", "d")
    second = ClientIdentity("a", "bc", "d")

    assert PostgresClientStateStore._advisory_lock_key(
        first
    ) != PostgresClientStateStore._advisory_lock_key(second)


def test_advisory_lock_key_preserves_separator_characters_without_ambiguity() -> None:
    first = ClientIdentity("telegram", "chat:1|user", "2")
    second = ClientIdentity("telegram", "chat:1", "user|2")

    assert PostgresClientStateStore._advisory_lock_key(
        first
    ) != PostgresClientStateStore._advisory_lock_key(second)


def test_activate_sends_no_nul_byte_in_advisory_lock_key_and_keeps_semantics() -> None:
    conversation_id = uuid4()
    fake_conn = FakeConnection(conversation_id)

    @contextmanager
    def fake_connect(settings):
        yield fake_conn

    identity = ClientIdentity(" telegram ", "chat:1|user", "user\n1")
    store = PostgresClientStateStore(
        Settings("postgresql://example", "knowledge", "unused", 384),
        connection_factory=fake_connect,
    )

    state = store.activate(identity, conversation_id)

    lock_query, lock_params = fake_conn.calls[0]
    assert lock_query == "SELECT pg_advisory_xact_lock(hashtext(%s))"
    assert len(lock_params) == 1
    assert "\0" not in lock_params[0]
    assert state.identity == identity
    assert state.active_conversation_id == conversation_id
    assert state.version == 1
    assert fake_conn.calls[1][1] == ("telegram", "chat:1|user", "user\n1", conversation_id)
    assert fake_conn.calls[2][1] == ("telegram", "chat:1|user", "user\n1", conversation_id)
