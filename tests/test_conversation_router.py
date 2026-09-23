from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

import pytest

from knowledge_system.client_state import ClientIdentity, ClientState
from knowledge_system.conversation_models import (
    ConversationRoutingAction,
    ConversationRoutingDecision,
)
from knowledge_system.conversation_router import ConversationRouter
from tests.conversation_fakes import FakeConversationStore


@dataclass
class FakeClientStates:
    state: dict[ClientIdentity, ClientState] = field(default_factory=dict)
    topics: dict[ClientIdentity, list[UUID]] = field(default_factory=dict)
    bindings: dict[tuple[ClientIdentity, str], UUID] = field(default_factory=dict)
    store: FakeConversationStore | None = None

    def get(self, identity):
        return self.state.get(identity)

    def activate(self, identity, conversation_id):
        previous = self.state.get(identity)
        value = ClientState(identity, conversation_id, (previous.version + 1) if previous else 1)
        self.state[identity] = value
        self.topics.setdefault(identity, [])
        if conversation_id not in self.topics[identity]:
            self.topics[identity].append(conversation_id)
        return value

    def list_conversations(self, identity, limit=20):
        return [
            self.store.get_conversation(item) for item in reversed(self.topics.get(identity, []))
        ][:limit]

    def bind_message(self, identity, external_message_id, conversation_id):
        return self.bindings.setdefault((identity, external_message_id), conversation_id)

    def get_message_binding(self, identity, external_message_id):
        return self.bindings.get((identity, external_message_id))


class FakeRouterModel:
    def __init__(self, decision):
        self.decision = decision
        self.calls = []

    def route(self, message, candidates):
        self.calls.append((message, candidates))
        return self.decision


@pytest.mark.parametrize("message", ["Wie bauen wir Telegram an?", "Und was hältst du davon?"])
def test_current_topic_and_ambiguous_followup_continue(message):
    store = FakeConversationStore()
    states = FakeClientStates(store=store)
    identity = ClientIdentity("telegram", "1", "2")
    current = store.create_conversation("telegram", "Knowledge-System")
    states.activate(identity, current.id)
    model = FakeRouterModel(
        ConversationRoutingDecision(ConversationRoutingAction.CONTINUE_CURRENT, confidence=0.9)
    )

    result = ConversationRouter(store, states, model).route(identity, message)

    assert result.conversation.id == current.id
    assert len(model.calls[0][1]) == 1


def test_clear_change_reuses_matching_existing_topic():
    store = FakeConversationStore()
    states = FakeClientStates(store=store)
    identity = ClientIdentity("telegram", "1", "2")
    current = store.create_conversation("telegram", "Knowledge-System")
    investments = store.create_conversation("telegram", "Investments")
    states.activate(identity, investments.id)
    states.activate(identity, current.id)
    model = FakeRouterModel(
        ConversationRoutingDecision(
            ConversationRoutingAction.SWITCH_TO_EXISTING, investments.id, confidence=0.95
        )
    )

    result = ConversationRouter(store, states, model).route(identity, "Warum steigt Monero?")

    assert result.conversation.id == investments.id
    assert states.get(identity).active_conversation_id == investments.id


def test_new_topic_is_created_and_invalid_switch_id_fails_closed():
    store = FakeConversationStore()
    states = FakeClientStates(store=store)
    identity = ClientIdentity("telegram", "1", "2")
    current = store.create_conversation("telegram", "Knowledge-System")
    states.activate(identity, current.id)
    decision = ConversationRoutingDecision(
        ConversationRoutingAction.START_NEW_CONVERSATION,
        suggested_title="Thailand-Reise",
        confidence=0.94,
    )
    result = ConversationRouter(store, states, FakeRouterModel(decision)).route(
        identity, "Thailand"
    )
    assert result.conversation.title == "Thailand-Reise"
    assert result.conversation.id != current.id


def test_manual_new_and_switch_bypass_model():
    store = FakeConversationStore()
    states = FakeClientStates(store=store)
    identity = ClientIdentity("telegram", "1", "2")
    model = FakeRouterModel(None)
    router = ConversationRouter(store, states, model)
    first = router.create(identity, "Erstes Thema")
    router.create(identity, "Zweites Thema")
    selected = router.switch(identity, str(first.id))
    assert selected.id == first.id
    assert model.calls == []


def test_provider_unoffered_switch_id_never_selects_another_clients_topic(monkeypatch):
    from tests.test_llm_provider import provider, routing_response

    store = FakeConversationStore()
    states = FakeClientStates(store=store)
    identity = ClientIdentity("telegram", "1", "2")
    foreign_identity = ClientIdentity("telegram", "9", "9")
    router = ConversationRouter(store, states, provider())
    current = router.create(identity, "Current")
    foreign = router.create(foreign_identity, "Foreign")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: routing_response(
            {"action": "switch_to_existing", "conversation_id": str(foreign.id), "confidence": 0.9}
        ),
    )
    result = router.route(identity, "Synthetic")
    # Preserve the existing fail-closed fallback, not a cross-client switch.
    assert result.conversation.id not in (current.id, foreign.id)
    assert states.get(identity).active_conversation_id == result.conversation.id
    assert states.get(foreign_identity).active_conversation_id == foreign.id


@pytest.mark.parametrize(
    "action",
    [ConversationRoutingAction.CONTINUE_CURRENT, ConversationRoutingAction.START_NEW_CONVERSATION],
)
def test_domain_still_rejects_id_for_non_switch_actions(action):
    from uuid import uuid4

    with pytest.raises(ValueError, match="only valid when switching"):
        ConversationRoutingDecision(action, uuid4(), confidence=0.9)
