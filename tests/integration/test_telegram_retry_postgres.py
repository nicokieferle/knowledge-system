"""Real stores/schema, synthetic providers; explicitly opt in to a local test DB."""

import os
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from knowledge_system.client_state import ClientIdentity, PostgresClientStateStore
from knowledge_system.config import Settings
from knowledge_system.conversation_models import (
    ConversationIntent,
    ConversationRoutingAction,
    ConversationRoutingDecision,
    MessageRole,
)
from knowledge_system.conversation_router import ConversationRouter
from knowledge_system.db import init_db
from knowledge_system.telegram_adapter import TelegramAdapter, TelegramCallback, TelegramMessage
from scripts.conversation_postgres_smoke import FakeIntentClassifier, _build_service
from tests.test_conversation_router import FakeRouterModel
from tests.test_telegram_adapter import FakeTransport


@pytest.fixture
def database():
    address = os.getenv("TEST_V311_DATABASE_URL")
    if not address:
        pytest.skip("TEST_V311_DATABASE_URL requires an isolated local PostgreSQL instance")
    parsed = urlsplit(address)
    assert parsed.hostname in ("127.0.0.1", "localhost")
    assert parsed.path.startswith("/knowledge_v311_test")
    assert not parsed.query, "Supply a plain local test URL without connection options"
    schema = "v311_" + uuid4().hex
    with psycopg.connect(address, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    query = urlencode([*parse_qsl(parsed.query), ("options", f"-csearch_path={schema},public")])
    settings = Settings(urlunsplit(parsed._replace(query=query)), Path("knowledge"), "unused", 384)
    try:
        init_db(settings)
        yield settings
    finally:
        # Only this fixture's newly generated schema, never shared/public data.
        with psycopg.connect(address, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def build(settings, intent=ConversationIntent.CHAT):
    service = _build_service(settings, FakeIntentClassifier(intent))
    states = PostgresClientStateStore(settings)
    model = FakeRouterModel(
        ConversationRoutingDecision(
            ConversationRoutingAction.START_NEW_CONVERSATION,
            suggested_title="Synthetic",
            confidence=1,
        )
    )
    adapter = TelegramAdapter(
        ConversationRouter(service.conversation_store, states, model),
        service,
        states,
        FakeTransport(),
    )
    return adapter


def fail(*args, **kwargs):
    raise RuntimeError("synthetic interruption")


def counts(settings):
    tables = ("conversations", "messages", "proposal_suggestions", "proposals")
    with psycopg.connect(settings.database_url) as conn:
        return tuple(
            conn.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
            ).fetchone()[0]
            for table in tables
        )


IDENTITY = ClientIdentity("telegram", "10", "20")
MESSAGE = TelegramMessage("10", "20", "30", "Synthetic insight")


def test_activation_binding_and_external_message_uniqueness(database):
    adapter = build(database)
    store, states = adapter.service.conversation_store, adapter.states
    first, second = store.create_conversation("telegram"), store.create_conversation("telegram")
    assert states.activate(IDENTITY, first.id).version == 1
    assert states.activate(IDENTITY, second.id).version == 2
    assert states.bind_message(IDENTITY, "30", first.id) == first.id
    assert states.bind_message(IDENTITY, "30", second.id) == first.id
    assert PostgresClientStateStore(database).get_message_binding(IDENTITY, "30") == first.id
    assert states.get_message_binding(replace(IDENTITY, external_user_id="other"), "30") is None
    a = store.add_message(first.id, MessageRole.USER, "Synthetic", "external")
    b = store.add_message(first.id, MessageRole.USER, "Synthetic", "external")
    assert a.id == b.id
    assert len(store.list_messages(first.id)) == 1
    assert store.find_external_message(second.id, "external") is None
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        states.bind_message(IDENTITY, "bad", uuid4())


@pytest.mark.parametrize("phase", ["route", "binding", "user", "suggestion", "assistant", "send"])
def test_retry_at_each_boundary_with_new_stores(database, monkeypatch, phase):
    intent = (
        ConversationIntent.SUGGEST_PROPOSAL if phase == "suggestion" else ConversationIntent.CHAT
    )
    adapter = build(database, intent)
    if phase == "route":
        monkeypatch.setattr(adapter.router.model, "route", fail)
    elif phase == "binding":
        monkeypatch.setattr(adapter.service, "handle_user_message", fail)
    elif phase == "user":
        monkeypatch.setattr(adapter.service.intent_classifier, "classify", fail)
    elif phase == "suggestion":
        original = adapter.service.conversation_store.add_message

        def insert(*args, **kwargs):
            if args[1] is MessageRole.ASSISTANT:
                fail()
            return original(*args, **kwargs)

        monkeypatch.setattr(adapter.service.conversation_store, "add_message", insert)
    else:
        monkeypatch.setattr(adapter.transport, "send_message", fail)
    with pytest.raises(RuntimeError):
        adapter.handle_message(MESSAGE)
    bound = adapter.states.get_message_binding(IDENTITY, "30")
    assert (
        counts(database)
        == {
            "route": (0, 0, 0, 0),
            "binding": (1, 0, 0, 0),
            "user": (1, 1, 0, 0),
            "suggestion": (1, 1, 1, 0),
            "assistant": (1, 2, 0, 0),
            "send": (1, 2, 0, 0),
        }[phase]
    )
    restarted = build(database, intent)
    if bound:
        monkeypatch.setattr(restarted.router.model, "route", fail)
    if phase in ("suggestion", "assistant", "send"):
        monkeypatch.setattr(restarted.service.intent_classifier, "classify", fail)
        monkeypatch.setattr(restarted.service.chat_model, "generate", fail)
    restarted.handle_message(MESSAGE)
    restarted.handle_message(MESSAGE)
    assert counts(database) == (1, 2, 1 if phase == "suggestion" else 0, 0)
    assert restarted.states.get_message_binding(IDENTITY, "30") == (
        bound or restarted.states.get(IDENTITY).active_conversation_id
    )
    assert restarted.transport.messages[0] == restarted.transport.messages[1]


def test_pre_binding_crash_documents_orphan_topic_limit(database, monkeypatch):
    adapter = build(database)
    monkeypatch.setattr(adapter.states, "bind_message", fail)
    with pytest.raises(RuntimeError):
        adapter.handle_message(MESSAGE)
    assert counts(database) == (1, 0, 0, 0)
    assert adapter.states.get_message_binding(IDENTITY, "30") is None
    restarted = build(database)
    restarted.handle_message(MESSAGE)
    # Routing has no durable decision key yet: an unused topic is possible.
    assert counts(database) == (2, 2, 0, 0)


def test_explicit_proposal_send_failure_reuses_proposal_after_restart(database, monkeypatch):
    adapter = build(database)
    message = replace(MESSAGE, text="/remember Synthetic")
    monkeypatch.setattr(adapter.transport, "send_message", fail)
    with pytest.raises(RuntimeError):
        adapter.handle_message(message)
    assert counts(database) == (1, 1, 0, 1)
    restarted = build(database)
    monkeypatch.setattr(restarted.router.model, "route", fail)
    monkeypatch.setattr(restarted.service.proposal_service.generator, "generate", fail)
    restarted.handle_message(message)
    restarted.handle_message(message)
    assert counts(database) == (1, 1, 0, 1)
    assert restarted.transport.messages[0] == restarted.transport.messages[1]


@pytest.mark.parametrize(
    "action,status,proposals", [("save", "confirmed", 1), ("reject", "rejected", 0)]
)
def test_callback_retry_after_commit_and_restart(database, monkeypatch, action, status, proposals):
    adapter = build(database, ConversationIntent.SUGGEST_PROPOSAL)
    adapter.handle_message(MESSAGE)
    cid = adapter.states.get_message_binding(IDENTITY, "30")
    user = adapter.service.conversation_store.find_external_message(cid, "telegram:message:30")
    suggestion = adapter.service.proposal_service.proposal_store.find_suggestion(cid, user.id)
    callback = TelegramCallback("cb-1", "10", "20", "99", f"{action}:{suggestion.id}")
    monkeypatch.setattr(adapter.transport, "answer_callback", fail)
    with pytest.raises(RuntimeError):
        adapter.handle_callback(callback)
    assert counts(database) == (1, 3, 1, proposals)
    restarted = build(database)
    monkeypatch.setattr(restarted.service.proposal_service.generator, "generate", fail)
    monkeypatch.setattr(restarted.service.knowledge_retriever, "search", fail)
    restarted.handle_callback(callback)
    restarted.handle_callback(callback)
    assert counts(database) == (1, 3, 1, proposals)
    store = restarted.service.proposal_service.proposal_store
    resolved = store.get_suggestion(cid, suggestion.id)
    assert resolved.status.value == status
    if action == "save":
        assert (
            store.find_proposal(cid, resolved.resolution_message_id).source_suggestion_id
            == suggestion.id
        )
    assert restarted.transport.callbacks[0] == restarted.transport.callbacks[1]


def test_new_command_binding_survives_restart(database, monkeypatch):
    adapter = build(database)
    message = replace(MESSAGE, text="/new Synthetic")
    monkeypatch.setattr(adapter.transport, "send_message", fail)
    with pytest.raises(RuntimeError):
        adapter.handle_message(message)
    build(database).handle_message(message)
    assert counts(database) == (1, 0, 0, 0)
