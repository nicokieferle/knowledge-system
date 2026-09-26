from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from knowledge_system.client_state import ClientIdentity, PostgresClientStateStore
from knowledge_system.conversation_store import PostgresConversationStore
from knowledge_system.proposal_store import PostgresProposalStore


@pytest.mark.parametrize("found", [False, True])
def test_binding_lookup_scopes_all_identity_fields(found):
    conn = MagicMock()
    conn.__enter__.return_value = conn
    cid = uuid4()
    conn.execute.return_value.fetchone.return_value = (cid,) if found else None
    store = PostgresClientStateStore(None, lambda _: conn)
    identity = ClientIdentity("telegram", "chat", "user")
    assert store.get_message_binding(identity, "message") == (cid if found else None)
    query, params = conn.execute.call_args.args
    assert params == ("telegram", "chat", "user", "message")
    assert "external_user_id=%s" in query
    assert "external_chat_id=%s" in query


@pytest.mark.parametrize("kind", ["proposal", "suggestion", "external_message"])
@pytest.mark.parametrize("found", [False, True])
def test_durable_lookup_scopes_conversation_and_handles_missing(monkeypatch, kind, found):
    conn = MagicMock()
    conn.__enter__.return_value = conn
    row, result, cid = ("persisted",), object(), uuid4()
    conn.execute.return_value.fetchone.return_value = row if found else None
    if kind == "external_message":
        store = PostgresConversationStore(None, lambda _: conn)
        module, converter, key = "conversation_store", "_message_from_row", "response:telegram:1"
    else:
        store = PostgresProposalStore(None, lambda _: conn)
        module, converter, key = "proposal_store", f"_{kind}_from_row", 42
    monkeypatch.setattr(f"knowledge_system.{module}.{converter}", lambda value: result)
    assert getattr(store, f"find_{kind}")(cid, key) is (result if found else None)
    query, params = conn.execute.call_args.args
    assert "conversation_id = %s" in query
    assert params == (cid, key)
