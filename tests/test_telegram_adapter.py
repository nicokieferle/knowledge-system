from __future__ import annotations

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
        self.callbacks = []

    def send_message(self, chat_id, text, buttons=()):
        self.messages.append((chat_id, text, buttons))

    def answer_callback(self, callback_id, text):
        self.callbacks.append((callback_id, text))


def adapter_for(intent=ConversationIntent.CHAT):
    service, store, proposals, classifier, chat, generator, retriever, _ = _service(intent=intent)
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
    assert "Vorschlag erstellt" in transport.messages[-1][1]


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
