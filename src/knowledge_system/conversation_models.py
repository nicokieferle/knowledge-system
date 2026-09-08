from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol
from uuid import UUID

RetrievalMode = Literal["fast", "quality"]


class KnowledgeResult(Protocol):
    chunk_key: str
    source_id: str
    source_path: str
    heading_path: str
    content: str
    score: float
    retrieval_mode: RetrievalMode


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ConversationIntent(StrEnum):
    CHAT = "chat"
    CREATE_PROPOSAL = "create_proposal"
    SUGGEST_PROPOSAL = "suggest_proposal"
    UNCERTAIN = "uncertain"


class ConversationAction(StrEnum):
    CHAT = "chat"
    PROPOSAL_CREATED = "proposal_created"
    PROPOSAL_CONFIRMATION_REQUIRED = "proposal_confirmation_required"
    SUGGESTION_REJECTED = "suggestion_rejected"


class ConversationRoutingAction(StrEnum):
    CONTINUE_CURRENT = "continue_current"
    SWITCH_TO_EXISTING = "switch_to_existing"
    START_NEW_CONVERSATION = "start_new_conversation"


class SuggestionStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class ProposalTriggerType(StrEnum):
    COMMAND = "command"
    INTENT = "intent"
    CONFIRMED_SUGGESTION = "confirmed_suggestion"


@dataclass(frozen=True)
class Conversation:
    id: UUID
    client_type: str
    title: str | None
    external_conversation_id: str | None
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None


@dataclass(frozen=True)
class ConversationTopic:
    conversation_id: UUID
    title: str
    topic_summary: str | None
    updated_at: datetime
    is_active: bool = False


@dataclass(frozen=True)
class ConversationRoutingDecision:
    action: ConversationRoutingAction
    conversation_id: UUID | None = None
    suggested_title: str | None = None
    confidence: float = 0.0

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 1:
            raise ValueError("routing confidence must be between 0 and 1")
        if self.action is ConversationRoutingAction.SWITCH_TO_EXISTING:
            if self.conversation_id is None:
                raise ValueError("switch decisions require conversation_id")
        elif self.conversation_id is not None:
            raise ValueError("conversation_id is only valid when switching")


@dataclass(frozen=True)
class Message:
    id: int
    conversation_id: UUID
    role: MessageRole
    content: str
    created_at: datetime
    external_message_id: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ConversationSummary:
    id: UUID
    conversation_id: UUID
    summary: str
    through_message_id: int
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ConversationContext:
    conversation_id: UUID
    conversation_summary: str | None
    recent_messages: tuple[Message, ...]
    relevant_knowledge: tuple[KnowledgeResult, ...]
    current_user_message: Message

    def __post_init__(self) -> None:
        if self.current_user_message.role is not MessageRole.USER:
            raise ValueError("current_user_message must have role user")
        if any(message.conversation_id != self.conversation_id for message in self.recent_messages):
            raise ValueError("recent_messages must belong to the context conversation")
        if self.current_user_message.conversation_id != self.conversation_id:
            raise ValueError("current_user_message must belong to the context conversation")
        if any(message.id == self.current_user_message.id for message in self.recent_messages):
            raise ValueError("recent_messages must exclude current_user_message")


@dataclass(frozen=True)
class ProposalDraft:
    summary: str
    reason: str
    proposed_content: str
    title: str | None = None
    target_source_id: str | None = None
    target_source_path: str | None = None
    base_revision: str | None = None


@dataclass(frozen=True)
class ProposalGenerationContext:
    conversation_id: UUID
    trigger_message_id: int
    trigger_type: ProposalTriggerType
    conversation_summary: str | None
    originating_messages: tuple[Message, ...]
    relevant_knowledge: tuple[KnowledgeResult, ...]

    def __post_init__(self) -> None:
        if any(
            message.conversation_id != self.conversation_id for message in self.originating_messages
        ):
            raise ValueError("originating_messages must belong to the proposal conversation")
        if any(message.id == self.trigger_message_id for message in self.originating_messages):
            raise ValueError("trigger message must not be proposal content context")


@dataclass(frozen=True)
class ProposalSuggestion:
    id: UUID
    conversation_id: UUID
    trigger_message_id: int
    originating_message_ids: tuple[int, ...]
    context_summary: str | None
    status: SuggestionStatus
    created_at: datetime
    resolved_at: datetime | None = None
    resolution_message_id: int | None = None


@dataclass(frozen=True)
class Proposal:
    id: UUID
    conversation_id: UUID
    status: str
    source_client: str
    trigger_type: ProposalTriggerType
    trigger_message_id: int
    originating_message_ids: tuple[int, ...]
    summary: str
    reason: str
    proposed_content: str
    title: str | None
    target_source_id: str | None
    target_source_path: str | None
    base_revision: str | None
    source_suggestion_id: UUID | None
    created_at: datetime


@dataclass(frozen=True)
class ConversationTurnResult:
    conversation_id: UUID
    user_message_id: int
    action: ConversationAction
    assistant_message: Message | None = None
    proposal_id: UUID | None = None
    pending_action_id: UUID | None = None


class ChatModel(Protocol):
    def generate(self, context: ConversationContext) -> str: ...


class ConversationSummarizer(Protocol):
    def summarize(self, previous_summary: str | None, messages: tuple[Message, ...]) -> str: ...


class IntentClassifier(Protocol):
    def classify(
        self,
        current_user_message: Message,
        recent_messages: tuple[Message, ...],
    ) -> ConversationIntent: ...


class ProposalGenerator(Protocol):
    def generate(self, context: ProposalGenerationContext) -> ProposalDraft: ...


class KnowledgeRetriever(Protocol):
    def search(
        self,
        query: str,
        mode: RetrievalMode = "quality",
        limit: int = 5,
    ) -> list[KnowledgeResult]: ...


class ConversationRoutingModel(Protocol):
    def route(
        self,
        current_user_message: str,
        candidates: tuple[ConversationTopic, ...],
    ) -> ConversationRoutingDecision: ...
