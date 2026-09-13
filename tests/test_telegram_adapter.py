from __future__ import annotations

import pytest

from knowledge_system.client_state import ClientIdentity
from knowledge_system.conversation_models import (
    ConversationIntent,
    ConversationRoutingAction,
    ConversationRoutingDecision,
)
from knowledge_system.conversation_router import ConversationRouter
from knowledge_system.telegram_adapter import TelegramAdapter, TelegramCallback, TelegramMessage
from tests.test_conversation_router import FakeClientStates, FakeRouterModel
from tests.test_conversation_service import _service


class FakeTransport:
    def __init__(self):
        self.messages = []
        self.documents = []
        self.callbacks = []

    def send_message(self, chat_id, text, buttons=()):
        self.messages.append((chat_id, text, buttons))

    def send_document(self, chat_id, filename, content, mime_type="text/x-diff"):
        self.documents.append((chat_id, filename, content, mime_type))

    def answer_callback(self, callback_id, text):
        self.callbacks.append((callback_id, text))


def adapter_for(intent=ConversationIntent.CHAT):
    service, store, proposals, classifier, chat, _generator, _retriever, _ = _service(intent=intent)
    states = FakeClientStates(store=store)
    model = FakeRouterModel(
        ConversationRoutingDecision(ConversationRoutingAction.CONTINUE_CURRENT, confidence=0.9)
    )
    transport = FakeTransport()
    adapter = TelegramAdapter(ConversationRouter(store, states, model), service, states, transport)
    return adapter, service, store, states, transport, proposals, classifier, chat


def test_normal_message_retry_and_restart_keep_topic_and_one_message():
    adapter, service, store, states, transport, _, _, _ = adapter_for()
    adapter.handle_message(TelegramMessage("10", "20", "30", "Hallo"))
    identity = ClientIdentity("telegram", "10", "20")
    conversation_id = states.get(identity).active_conversation_id
    restarted = TelegramAdapter(adapter.router, service, states, transport)
    restarted.handle_message(TelegramMessage("10", "20", "30", "Hallo"))
    user_messages = [m for m in store.list_messages(conversation_id) if m.role.value == "user"]
    assistant_messages = [
        m for m in store.list_messages(conversation_id) if m.role.value == "assistant"
    ]
    assert len(user_messages) == 1
    assert len(assistant_messages) == 1


@pytest.mark.parametrize(
    "intent,text",
    [
        (ConversationIntent.CHAT, "Hallo"),
        (ConversationIntent.SUGGEST_PROPOSAL, "Insight"),
        (ConversationIntent.CREATE_PROPOSAL, "/remember insight"),
    ],
)
def test_send_failure_replays_durable_result_without_model_or_duplicates(intent, text):
    adapter, service, store, states, transport, proposals, classifier, chat = adapter_for(intent)
    original_send = transport.send_message

    def fail(*args, **kwargs):
        raise RuntimeError("send failed")

    transport.send_message = fail
    message = TelegramMessage("10", "20", "30", text)
    with pytest.raises(RuntimeError):
        adapter.handle_message(message)
    classifier.classify = fail
    chat.generate = fail
    adapter.router.model.route = fail
    service.proposal_service.generator.generate = fail
    transport.send_message = original_send
    restarted = TelegramAdapter(adapter.router, service, states, transport)
    restarted.handle_message(message)
    restarted.handle_message(message)
    assert len(store.state.conversations) == 1
    messages = next(iter(store.state.messages.values()))
    assert len([m for m in messages if m.role.value == "user"]) == 1
    assert len([m for m in messages if m.role.value == "assistant"]) == (
        0 if text.startswith("/remember") else 1
    )
    assert len(proposals.state.suggestions) == (
        1 if intent is ConversationIntent.SUGGEST_PROPOSAL else 0
    )
    assert len(proposals.state.proposals) == (
        1 if intent is ConversationIntent.CREATE_PROPOSAL else 0
    )
    assert transport.messages[0] == transport.messages[1]


@pytest.mark.parametrize("action", ["save", "reject"])
def test_callback_delivery_failure_retry(action):
    adapter, service, store, _, transport, proposals, _, _ = adapter_for(
        ConversationIntent.SUGGEST_PROPOSAL
    )
    adapter.handle_message(TelegramMessage("10", "20", "30", "Insight"))
    sid = next(iter(proposals.state.suggestions))
    callback = TelegramCallback("cb-1", "10", "20", "99", f"{action}:{sid}")
    original = transport.answer_callback

    def fail(*args, **kwargs):
        raise RuntimeError("delivery failed")

    transport.answer_callback = fail
    with pytest.raises(RuntimeError):
        adapter.handle_callback(callback)
    service.proposal_service.generator.generate = fail
    transport.answer_callback = original
    adapter.handle_callback(callback)
    assert len(proposals.state.proposals) == (1 if action == "save" else 0)
    messages = next(iter(store.state.messages.values()))
    assert len([m for m in messages if m.external_message_id == "telegram:callback:cb-1"]) == 1


def test_new_command_retry_reuses_bound_topic():
    adapter, _, store, _, _, _, _, _ = adapter_for()
    message = TelegramMessage("10", "20", "30", "/new Notes")
    adapter.handle_message(message)
    adapter.handle_message(message)
    assert len(store.state.conversations) == 1


def test_retry_recovers_suggestion_committed_before_assistant_failure():
    adapter, _, store, _, _, proposals, classifier, _ = adapter_for(
        ConversationIntent.SUGGEST_PROPOSAL
    )
    original = store.add_message

    def fail_assistant(conversation_id, role, *args, **kwargs):
        if role.value == "assistant":
            raise RuntimeError("DB unavailable")
        return original(conversation_id, role, *args, **kwargs)

    store.add_message = fail_assistant
    message = TelegramMessage("10", "20", "30", "Insight")
    with pytest.raises(RuntimeError):
        adapter.handle_message(message)
    assert len(proposals.state.suggestions) == 1
    store.add_message = original
    classifier.intent = ConversationIntent.CREATE_PROPOSAL
    adapter.handle_message(message)
    assert len(classifier.calls) == 1
    assert len(proposals.state.suggestions) == 1
    assert len(proposals.state.proposals) == 0
    assert len(next(iter(store.state.messages.values()))) == 2


def test_classifier_failure_retries_one_user_message_in_original_topic():
    adapter, _, store, _, _, _, classifier, _ = adapter_for()
    original = classifier.classify

    def fail(*args):
        raise RuntimeError("provider failed")

    classifier.classify = fail
    message = TelegramMessage("10", "20", "30", "Insight")
    with pytest.raises(RuntimeError):
        adapter.handle_message(message)
    classifier.classify = original
    adapter.handle_message(message)
    assert len(store.state.conversations) == 1
    assert len(next(iter(store.state.messages.values()))) == 2


def test_topics_new_and_switch_are_manual():
    adapter, _, _, states, transport, _, _, _ = adapter_for()
    adapter.handle_message(TelegramMessage("10", "20", "1", "/new Knowledge-System"))
    first = states.get(ClientIdentity("telegram", "10", "20")).active_conversation_id
    adapter.handle_message(TelegramMessage("10", "20", "2", "/new Investments"))
    adapter.handle_message(TelegramMessage("10", "20", "3", "/topics"))
    adapter.handle_message(TelegramMessage("10", "20", "4", f"/switch {first}"))
    assert "Investments" in transport.messages[-2][1]
    assert "Knowledge-System" in transport.messages[-2][1]
    assert states.get(ClientIdentity("telegram", "10", "20")).active_conversation_id == first


def test_remember_creates_proposal_without_chat_model():
    adapter, _, _, _, transport, proposals, _, chat = adapter_for()
    adapter.handle_message(TelegramMessage("10", "20", "1", "/new Notes"))
    adapter.handle_message(TelegramMessage("10", "20", "2", "/remember important"))
    assert len(proposals.state.proposals) == 1
    assert len(chat.contexts) == 0
    assert "Wissensvorschlag erstellt und zur Prüfung vorgemerkt." in transport.messages[-1][1]


def test_suggestion_buttons_confirmation_is_idempotent_and_reject_creates_none():
    adapter, _, _, _, transport, proposals, _, _ = adapter_for(ConversationIntent.SUGGEST_PROPOSAL)
    adapter.handle_message(TelegramMessage("10", "20", "1", "Potential insight"))
    buttons = transport.messages[-1][2]
    save_data = buttons[0][1]
    callback = TelegramCallback("cb-1", "10", "20", "99", save_data)
    adapter.handle_callback(callback)
    adapter.handle_callback(callback)
    assert len(proposals.state.proposals) == 1

    other, _, _, _, other_transport, other_proposals, _, _ = adapter_for(
        ConversationIntent.SUGGEST_PROPOSAL
    )
    other.handle_message(TelegramMessage("11", "21", "1", "Other insight"))
    reject_data = other_transport.messages[-1][2][1][1]
    other.handle_callback(TelegramCallback("cb-2", "11", "21", "99", reject_data))
    assert other_proposals.state.proposals == {}
