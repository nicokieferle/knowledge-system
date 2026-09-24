from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from .client_state import ClientIdentity, ClientStateStore
from .conversation_models import (
    Conversation,
    ConversationRoutingAction,
    ConversationRoutingDecision,
    ConversationRoutingModel,
    ConversationTopic,
)
from .conversation_store import ConversationNotFoundError, ConversationStore


@dataclass(frozen=True)
class RoutedConversation:
    conversation: Conversation
    decision: ConversationRoutingDecision


class ConversationRouter:
    def __init__(
        self,
        store: ConversationStore,
        client_states: ClientStateStore,
        model: ConversationRoutingModel,
        candidate_limit: int = 20,
    ) -> None:
        self.store, self.client_states, self.model = store, client_states, model
        self.candidate_limit = candidate_limit

    def route(self, identity: ClientIdentity, message: str) -> RoutedConversation:
        state = self.client_states.get(identity)
        conversations = self.client_states.list_conversations(identity, self.candidate_limit)
        active_id = state.active_conversation_id if state else None
        active = next((c for c in conversations if c.id == active_id), None)
        topics = tuple(
            ConversationTopic(c.id, c.title or "Unbenannt", None, c.updated_at, c.id == active_id)
            for c in conversations
        )
        decision = self.model.route(message, topics)
        conversation = self._resolve(identity, active, conversations, decision)
        self.client_states.activate(identity, conversation.id)
        return RoutedConversation(conversation, decision)

    def create(self, identity: ClientIdentity, title: str | None = None) -> Conversation:
        conversation = self.store.create_conversation(identity.client_type, title or "Neues Thema")
        self.client_states.activate(identity, conversation.id)
        return conversation

    def switch(self, identity: ClientIdentity, selector: str) -> Conversation:
        candidates = self.client_states.list_conversations(identity, self.candidate_limit)
        try:
            position = int(selector)
        except ValueError:
            position = 0
        selected = candidates[position - 1] if 1 <= position <= len(candidates) else None
        if selected is None:
            try:
                wanted = UUID(selector)
            except ValueError as exc:
                raise ConversationNotFoundError("Ungültiges Thema") from exc
            selected = next((item for item in candidates if item.id == wanted), None)
        if selected is None or selected.archived_at is not None:
            raise ConversationNotFoundError("Thema nicht gefunden oder archiviert")
        self.client_states.activate(identity, selected.id)
        return selected

    def _resolve(
        self,
        identity: ClientIdentity,
        active: Conversation | None,
        candidates: list[Conversation],
        decision: ConversationRoutingDecision,
    ) -> Conversation:
        if decision.action is ConversationRoutingAction.CONTINUE_CURRENT and active:
            return active
        if decision.action is ConversationRoutingAction.SWITCH_TO_EXISTING:
            match = next((c for c in candidates if c.id == decision.conversation_id), None)
            if match is not None and match.archived_at is None:
                return match
        # Invalid model IDs fail closed into a new isolated topic, never another user's topic.
        return self.store.create_conversation(
            identity.client_type, decision.suggested_title or "Neues Thema"
        )
