from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from .client_state import ClientIdentity, ClientStateStore
from .conversation_models import ConversationAction, ConversationTurnResult
from .conversation_router import ConversationRouter
from .conversation_service import ConversationService
from .conversation_store import ConversationNotFoundError
from .proposal_store import ProposalSuggestionNotFoundError, ProposalSuggestionStateError


class TelegramTransport(Protocol):
    def send_message(
        self, chat_id: str, text: str, buttons: tuple[tuple[str, str], ...] = ()
    ) -> None: ...

    def answer_callback(self, callback_id: str, text: str) -> None: ...


@dataclass(frozen=True)
class TelegramMessage:
    chat_id: str
    user_id: str
    message_id: str
    text: str


@dataclass(frozen=True)
class TelegramCallback:
    callback_id: str
    chat_id: str
    user_id: str
    message_id: str
    data: str


class TelegramAdapter:
    """Thin client adapter: parsing/control/formatting only; domain logic stays in services."""

    def __init__(
        self,
        router: ConversationRouter,
        service: ConversationService,
        states: ClientStateStore,
        transport: TelegramTransport,
    ) -> None:
        self.router, self.service, self.states, self.transport = router, service, states, transport

    def handle_message(self, message: TelegramMessage) -> None:
        identity = ClientIdentity("telegram", message.chat_id, message.user_id)
        text = message.text.strip()
        command, _, argument = text.partition(" ")
        if command == "/new":
            conversation = self.router.create(identity, argument.strip() or None)
            self.transport.send_message(message.chat_id, f"Neues Thema: {conversation.title}")
            return
        if command == "/topics":
            topics = self.states.list_conversations(identity)
            rendered = "\n".join(
                f"{i}. {c.title or 'Unbenannt'} — {c.id}" for i, c in enumerate(topics, 1)
            )
            self.transport.send_message(message.chat_id, rendered or "Noch keine Themen.")
            return
        if command == "/switch":
            try:
                conversation = self.router.switch(identity, argument.strip())
            except ConversationNotFoundError:
                self.transport.send_message(
                    message.chat_id, "Thema nicht gefunden oder archiviert."
                )
            else:
                self.transport.send_message(message.chat_id, f"Aktives Thema: {conversation.title}")
            return

        routed = self.router.route(identity, text)
        bound_id = self.states.bind_message(identity, message.message_id, routed.conversation.id)
        # A retry must use the original topic even if routing state changed meanwhile.
        result = self.service.handle_user_message(
            bound_id, text, external_message_id=f"telegram:message:{message.message_id}"
        )
        self._send_result(message.chat_id, result)

    def handle_callback(self, callback: TelegramCallback) -> None:
        identity = ClientIdentity("telegram", callback.chat_id, callback.user_id)
        try:
            action, raw_suggestion = callback.data.split(":", 1)
            suggestion_id = UUID(raw_suggestion)
            conversation_id = self._suggestion_conversation(identity, suggestion_id)
            if conversation_id is None:
                raise ValueError
            external_id = f"telegram:callback:{callback.callback_id}"
            if action == "save":
                result = self.service.confirm_suggestion(
                    conversation_id,
                    suggestion_id,
                    "Bestätigt",
                    external_message_id=external_id,
                )
                text = f"Vorschlag erstellt: {result.proposal_id}"
            elif action == "reject":
                self.service.reject_suggestion(
                    conversation_id,
                    suggestion_id,
                    "Abgelehnt",
                    external_message_id=external_id,
                )
                text = "Nicht gespeichert."
            else:
                raise ValueError
        except (ValueError, ProposalSuggestionStateError):
            text = "Diese Aktion ist ungültig oder bereits erledigt."
        self.transport.answer_callback(callback.callback_id, text)

    def _suggestion_conversation(
        self, identity: ClientIdentity, suggestion_id: UUID
    ) -> UUID | None:
        # The callback contains no conversation/content payload. Resolve only among topics
        # owned by this client identity, then let ProposalService validate pending state.
        for conversation in self.states.list_conversations(identity):
            try:
                self.service.proposal_service.get_suggestion(conversation.id, suggestion_id)
            except ProposalSuggestionNotFoundError:
                continue
            return conversation.id
        return None

    def _send_result(self, chat_id: str, result: ConversationTurnResult) -> None:
        if result.action is ConversationAction.PROPOSAL_CONFIRMATION_REQUIRED:
            sid = str(result.pending_action_id)
            self.transport.send_message(
                chat_id,
                result.assistant_message.content,
                (("Speichern", f"save:{sid}"), ("Nein", f"reject:{sid}")),
            )
        elif result.action is ConversationAction.PROPOSAL_CREATED:
            self.transport.send_message(chat_id, f"Vorschlag erstellt: {result.proposal_id}")
        elif result.assistant_message:
            self.transport.send_message(chat_id, result.assistant_message.content)
