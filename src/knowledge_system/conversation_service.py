from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from .conversation_memory import ConversationContextBuilder
from .conversation_models import (
    ChatModel,
    Conversation,
    ConversationAction,
    ConversationIntent,
    ConversationTurnResult,
    IntentClassifier,
    KnowledgeResult,
    KnowledgeRetriever,
    Message,
    MessageRole,
    ProposalTriggerType,
    RetrievalMode,
)
from .conversation_store import ConversationStore
from .proposal_service import ProposalService
from .proposal_store import ProposalSuggestionStateError

EXPLICIT_PROPOSAL_COMMANDS = frozenset({"/remember", "/propose"})
SUGGESTION_CONFIRMATION_TEXT = (
    "Das könnte sich für deine Wissensbasis eignen. Soll ich daraus einen Wissensvorschlag machen?"
)


@dataclass(frozen=True)
class ConversationRetrievalPolicy:
    mode: RetrievalMode = "quality"
    limit: int = 5

    def __post_init__(self) -> None:
        if self.mode not in ("fast", "quality"):
            raise ValueError("retrieval mode must be fast or quality")
        if self.limit < 1:
            raise ValueError("retrieval limit must be at least 1")


class ConversationService:
    def __init__(
        self,
        *,
        conversation_store: ConversationStore,
        context_builder: ConversationContextBuilder,
        proposal_service: ProposalService,
        chat_model: ChatModel,
        intent_classifier: IntentClassifier,
        knowledge_retriever: KnowledgeRetriever,
        retrieval_policy: ConversationRetrievalPolicy | None = None,
    ) -> None:
        self.conversation_store = conversation_store
        self.context_builder = context_builder
        self.proposal_service = proposal_service
        self.chat_model = chat_model
        self.intent_classifier = intent_classifier
        self.knowledge_retriever = knowledge_retriever
        self.retrieval_policy = retrieval_policy or ConversationRetrievalPolicy()

    def create_conversation(
        self,
        client_type: str = "internal",
        title: str | None = None,
        external_conversation_id: str | None = None,
    ) -> Conversation:
        return self.conversation_store.create_conversation(
            client_type,
            title,
            external_conversation_id,
        )

    def handle_user_message(
        self,
        conversation_id: UUID,
        content: str,
        *,
        external_message_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> ConversationTurnResult:
        conversation = self.conversation_store.get_conversation(conversation_id)
        user_message = self.conversation_store.add_message(
            conversation_id,
            MessageRole.USER,
            content,
            external_message_id,
            metadata,
        )
        history = self.context_builder.load_history(
            conversation_id,
            before_message_id=user_message.id,
        )

        first_token = content.strip().lower().split(maxsplit=1)[0]
        is_command = first_token in EXPLICIT_PROPOSAL_COMMANDS
        intent = (
            ConversationIntent.CREATE_PROPOSAL
            if is_command
            else self.intent_classifier.classify(user_message, history.recent_messages)
        )

        if intent is ConversationIntent.CREATE_PROPOSAL:
            knowledge = self._retrieve_for_messages(history.recent_messages)
            proposal = self.proposal_service.create_proposal(
                conversation_id=conversation_id,
                source_client=conversation.client_type,
                trigger_message_id=user_message.id,
                trigger_type=(
                    ProposalTriggerType.COMMAND if is_command else ProposalTriggerType.INTENT
                ),
                conversation_summary=history.summary,
                originating_messages=history.recent_messages,
                relevant_knowledge=knowledge,
            )
            return ConversationTurnResult(
                conversation_id=conversation_id,
                user_message_id=user_message.id,
                action=ConversationAction.PROPOSAL_CREATED,
                proposal_id=proposal.id,
            )

        if intent is ConversationIntent.SUGGEST_PROPOSAL:
            suggestion = self.proposal_service.create_suggestion(
                conversation_id=conversation_id,
                trigger_message_id=user_message.id,
                conversation_summary=history.summary,
                originating_messages=history.recent_messages,
            )
            assistant = self.conversation_store.add_message(
                conversation_id,
                MessageRole.ASSISTANT,
                SUGGESTION_CONFIRMATION_TEXT,
                metadata={"proposal_suggestion_id": str(suggestion.id)},
            )
            return ConversationTurnResult(
                conversation_id=conversation_id,
                user_message_id=user_message.id,
                assistant_message=assistant,
                action=ConversationAction.PROPOSAL_CONFIRMATION_REQUIRED,
                pending_action_id=suggestion.id,
            )

        knowledge = tuple(
            self.knowledge_retriever.search(
                user_message.content,
                mode=self.retrieval_policy.mode,
                limit=self.retrieval_policy.limit,
            )
        )
        context = self.context_builder.build(
            conversation_id,
            user_message,
            knowledge,
            history=history,
        )
        assistant_content = self.chat_model.generate(context)
        assistant = self.conversation_store.add_message(
            conversation_id,
            MessageRole.ASSISTANT,
            assistant_content,
            external_message_id=(
                f"response:{external_message_id}" if external_message_id is not None else None
            ),
            metadata={"reply_to_message_id": user_message.id},
        )
        return ConversationTurnResult(
            conversation_id=conversation_id,
            user_message_id=user_message.id,
            assistant_message=assistant,
            action=ConversationAction.CHAT,
        )

    def confirm_suggestion(
        self,
        conversation_id: UUID,
        suggestion_id: UUID,
        confirmation_content: str,
        *,
        external_message_id: str | None = None,
    ) -> ConversationTurnResult:
        conversation = self.conversation_store.get_conversation(conversation_id)
        suggestion = self.proposal_service.get_suggestion(conversation_id, suggestion_id)
        confirmation = self.conversation_store.add_message(
            conversation_id,
            MessageRole.USER,
            confirmation_content,
            external_message_id,
            {"proposal_suggestion_id": str(suggestion_id), "control": "confirm"},
        )
        self._validate_resolution_message(confirmation, suggestion_id, "confirm")
        originating_ids = tuple(
            dict.fromkeys((*suggestion.originating_message_ids, suggestion.trigger_message_id))
        )
        originating_messages = tuple(
            self.conversation_store.get_messages(conversation_id, originating_ids)
        )
        knowledge = self._retrieve_for_messages(originating_messages)
        proposal = self.proposal_service.confirm_suggestion(
            conversation_id=conversation_id,
            suggestion_id=suggestion_id,
            confirmation_message_id=confirmation.id,
            source_client=conversation.client_type,
            relevant_knowledge=knowledge,
        )
        return ConversationTurnResult(
            conversation_id=conversation_id,
            user_message_id=confirmation.id,
            action=ConversationAction.PROPOSAL_CREATED,
            proposal_id=proposal.id,
            pending_action_id=suggestion_id,
        )

    def reject_suggestion(
        self,
        conversation_id: UUID,
        suggestion_id: UUID,
        rejection_content: str,
        *,
        external_message_id: str | None = None,
    ) -> ConversationTurnResult:
        self.conversation_store.get_conversation(conversation_id)
        self.proposal_service.get_suggestion(conversation_id, suggestion_id)
        rejection = self.conversation_store.add_message(
            conversation_id,
            MessageRole.USER,
            rejection_content,
            external_message_id,
            {"proposal_suggestion_id": str(suggestion_id), "control": "reject"},
        )
        self._validate_resolution_message(rejection, suggestion_id, "reject")
        self.proposal_service.reject_suggestion(
            conversation_id=conversation_id,
            suggestion_id=suggestion_id,
            resolution_message_id=rejection.id,
        )
        return ConversationTurnResult(
            conversation_id=conversation_id,
            user_message_id=rejection.id,
            action=ConversationAction.SUGGESTION_REJECTED,
            pending_action_id=suggestion_id,
        )

    def _retrieve_for_messages(
        self,
        messages: tuple[Message, ...],
    ) -> tuple[KnowledgeResult, ...]:
        query = next(
            (
                message.content
                for message in reversed(messages)
                if message.role is MessageRole.USER and message.content.strip()
            ),
            "",
        )
        if not query:
            return ()
        return tuple(
            self.knowledge_retriever.search(
                query,
                mode=self.retrieval_policy.mode,
                limit=self.retrieval_policy.limit,
            )
        )

    @staticmethod
    def _validate_resolution_message(
        message: Message,
        suggestion_id: UUID,
        control: str,
    ) -> None:
        if (
            message.metadata.get("proposal_suggestion_id") != str(suggestion_id)
            or message.metadata.get("control") != control
        ):
            raise ProposalSuggestionStateError(
                "External message is already linked to another proposal action"
            )
