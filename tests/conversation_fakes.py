from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import Lock
from uuid import UUID, uuid4

from knowledge_system.conversation_models import (
    Conversation,
    ConversationContext,
    ConversationIntent,
    ConversationSummary,
    KnowledgeResult,
    Message,
    MessageRole,
    Proposal,
    ProposalDraft,
    ProposalGenerationContext,
    ProposalSuggestion,
    ProposalTriggerType,
    SuggestionStatus,
)
from knowledge_system.conversation_store import (
    ConversationNotFoundError,
    MessageNotFoundError,
)
from knowledge_system.proposal_store import (
    ProposalCreate,
    ProposalSuggestionNotFoundError,
    ProposalSuggestionStateError,
)


@dataclass(frozen=True)
class FakeKnowledgeResult:
    chunk_key: str
    source_id: str
    source_path: str
    heading_path: str
    content: str
    score: float
    retrieval_mode: str


@dataclass
class ConversationState:
    conversations: dict[UUID, Conversation] = field(default_factory=dict)
    messages: dict[UUID, list[Message]] = field(default_factory=dict)
    summaries: dict[UUID, ConversationSummary] = field(default_factory=dict)
    next_message_id: int = 1
    clock_tick: int = 0

    def now(self) -> datetime:
        value = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=self.clock_tick)
        self.clock_tick += 1
        return value


class FakeConversationStore:
    def __init__(self, state: ConversationState | None = None) -> None:
        self.state = state or ConversationState()
        self.force_summary_conflict = False

    def create_conversation(
        self,
        client_type: str,
        title: str | None = None,
        external_conversation_id: str | None = None,
    ) -> Conversation:
        now = self.state.now()
        conversation = Conversation(
            id=uuid4(),
            client_type=client_type,
            title=title,
            external_conversation_id=external_conversation_id,
            created_at=now,
            updated_at=now,
        )
        self.state.conversations[conversation.id] = conversation
        self.state.messages[conversation.id] = []
        return conversation

    def get_conversation(self, conversation_id: UUID) -> Conversation:
        try:
            return self.state.conversations[conversation_id]
        except KeyError as exc:
            raise ConversationNotFoundError(str(conversation_id)) from exc

    def list_conversations(self, *, limit: int = 20) -> list[Conversation]:
        return sorted(
            (item for item in self.state.conversations.values() if item.archived_at is None),
            key=lambda item: item.updated_at,
            reverse=True,
        )[:limit]

    def add_message(
        self,
        conversation_id: UUID,
        role: MessageRole,
        content: str,
        external_message_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> Message:
        self.get_conversation(conversation_id)
        if external_message_id is not None:
            existing = next(
                (
                    message
                    for message in self.state.messages[conversation_id]
                    if message.external_message_id == external_message_id
                ),
                None,
            )
            if existing:
                return existing
        message = Message(
            id=self.state.next_message_id,
            conversation_id=conversation_id,
            role=role,
            content=content,
            created_at=self.state.now(),
            external_message_id=external_message_id,
            metadata=metadata or {},
        )
        self.state.next_message_id += 1
        self.state.messages[conversation_id].append(message)
        return message

    def get_message(self, conversation_id: UUID, message_id: int) -> Message:
        messages = self.get_messages(conversation_id, (message_id,))
        return messages[0]

    def list_messages(
        self,
        conversation_id: UUID,
        *,
        after_message_id: int | None = None,
        before_message_id: int | None = None,
    ) -> list[Message]:
        self.get_conversation(conversation_id)
        return [
            message
            for message in self.state.messages[conversation_id]
            if (after_message_id is None or message.id > after_message_id)
            and (before_message_id is None or message.id < before_message_id)
        ]

    def get_messages(self, conversation_id: UUID, message_ids) -> list[Message]:
        wanted = set(message_ids)
        messages = [
            message for message in self.list_messages(conversation_id) if message.id in wanted
        ]
        if len(messages) != len(wanted):
            raise MessageNotFoundError("missing message")
        return messages

    def get_summary(self, conversation_id: UUID) -> ConversationSummary | None:
        self.get_conversation(conversation_id)
        return self.state.summaries.get(conversation_id)

    def compare_and_set_summary(
        self,
        conversation_id: UUID,
        *,
        expected_version: int,
        summary: str,
        through_message_id: int,
    ) -> bool:
        current = self.state.summaries.get(conversation_id)
        current_version = current.version if current else 0
        if self.force_summary_conflict or current_version != expected_version:
            return False
        now = self.state.now()
        self.state.summaries[conversation_id] = ConversationSummary(
            id=current.id if current else uuid4(),
            conversation_id=conversation_id,
            summary=summary,
            through_message_id=through_message_id,
            version=expected_version + 1,
            created_at=current.created_at if current else now,
            updated_at=now,
        )
        return True


@dataclass
class ProposalState:
    proposals: dict[UUID, Proposal] = field(default_factory=dict)
    suggestions: dict[UUID, ProposalSuggestion] = field(default_factory=dict)
    direct_keys: dict[tuple[UUID, int, ProposalTriggerType], UUID] = field(default_factory=dict)
    suggestion_trigger_keys: dict[tuple[UUID, int], UUID] = field(default_factory=dict)
    suggestion_proposals: dict[UUID, UUID] = field(default_factory=dict)


class FakeProposalStore:
    def __init__(self, state: ProposalState | None = None) -> None:
        self.state = state or ProposalState()
        self.fail_confirmation_once = False
        self._lock = Lock()

    def create_proposal(self, create: ProposalCreate) -> Proposal:
        key = (create.conversation_id, create.trigger_message_id, create.trigger_type)
        if key in self.state.direct_keys:
            return self.state.proposals[self.state.direct_keys[key]]
        proposal = self._proposal(create)
        self.state.proposals[proposal.id] = proposal
        self.state.direct_keys[key] = proposal.id
        return proposal

    def create_suggestion(
        self,
        conversation_id: UUID,
        trigger_message_id: int,
        originating_message_ids: tuple[int, ...],
        context_summary: str | None,
    ) -> ProposalSuggestion:
        key = (conversation_id, trigger_message_id)
        if key in self.state.suggestion_trigger_keys:
            return self.state.suggestions[self.state.suggestion_trigger_keys[key]]
        suggestion = ProposalSuggestion(
            id=uuid4(),
            conversation_id=conversation_id,
            trigger_message_id=trigger_message_id,
            originating_message_ids=originating_message_ids,
            context_summary=context_summary,
            status=SuggestionStatus.PENDING,
            created_at=datetime.now(UTC),
        )
        self.state.suggestions[suggestion.id] = suggestion
        self.state.suggestion_trigger_keys[key] = suggestion.id
        return suggestion

    def get_suggestion(
        self,
        conversation_id: UUID,
        suggestion_id: UUID,
    ) -> ProposalSuggestion:
        suggestion = self.state.suggestions.get(suggestion_id)
        if suggestion is None or suggestion.conversation_id != conversation_id:
            raise ProposalSuggestionNotFoundError(str(suggestion_id))
        return suggestion

    def confirm_suggestion(
        self,
        conversation_id: UUID,
        suggestion_id: UUID,
        confirmation_message_id: int,
        source_client: str,
        draft: ProposalDraft,
    ) -> Proposal:
        with self._lock:
            suggestion = self.get_suggestion(conversation_id, suggestion_id)
            if suggestion.status is SuggestionStatus.REJECTED:
                raise ProposalSuggestionStateError("rejected")
            if suggestion.status is SuggestionStatus.CONFIRMED:
                return self.state.proposals[self.state.suggestion_proposals[suggestion_id]]

            originating_ids = tuple(
                dict.fromkeys((*suggestion.originating_message_ids, suggestion.trigger_message_id))
            )
            create = ProposalCreate(
                conversation_id=conversation_id,
                source_client=source_client,
                trigger_type=ProposalTriggerType.CONFIRMED_SUGGESTION,
                trigger_message_id=confirmation_message_id,
                originating_message_ids=originating_ids,
                draft=draft,
                source_suggestion_id=suggestion_id,
            )
            trigger_key = (
                conversation_id,
                confirmation_message_id,
                ProposalTriggerType.CONFIRMED_SUGGESTION,
            )
            existing_trigger = self.state.direct_keys.get(trigger_key)
            if existing_trigger is not None:
                existing = self.state.proposals[existing_trigger]
                if existing.source_suggestion_id != suggestion_id:
                    raise ProposalSuggestionStateError(
                        "confirmation message already belongs to another suggestion"
                    )
            proposal = self._proposal(create)
            if self.fail_confirmation_once:
                self.fail_confirmation_once = False
                raise RuntimeError("simulated failure before transaction commit")

            now = datetime.now(UTC)
            confirmed = ProposalSuggestion(
                **{
                    **suggestion.__dict__,
                    "status": SuggestionStatus.CONFIRMED,
                    "resolved_at": now,
                    "resolution_message_id": confirmation_message_id,
                }
            )
            # These mutations represent one database transaction commit.
            self.state.proposals[proposal.id] = proposal
            self.state.direct_keys[trigger_key] = proposal.id
            self.state.suggestion_proposals[suggestion_id] = proposal.id
            self.state.suggestions[suggestion_id] = confirmed
            return proposal

    def reject_suggestion(
        self,
        conversation_id: UUID,
        suggestion_id: UUID,
        resolution_message_id: int,
    ) -> ProposalSuggestion:
        suggestion = self.get_suggestion(conversation_id, suggestion_id)
        if suggestion.status is SuggestionStatus.CONFIRMED:
            raise ProposalSuggestionStateError("confirmed")
        if suggestion.status is SuggestionStatus.REJECTED:
            return suggestion
        rejected = ProposalSuggestion(
            **{
                **suggestion.__dict__,
                "status": SuggestionStatus.REJECTED,
                "resolved_at": datetime.now(UTC),
                "resolution_message_id": resolution_message_id,
            }
        )
        self.state.suggestions[suggestion_id] = rejected
        return rejected

    @staticmethod
    def _proposal(create: ProposalCreate) -> Proposal:
        draft = create.draft
        return Proposal(
            id=uuid4(),
            conversation_id=create.conversation_id,
            status="pending",
            source_client=create.source_client,
            trigger_type=create.trigger_type,
            trigger_message_id=create.trigger_message_id,
            originating_message_ids=create.originating_message_ids,
            summary=draft.summary,
            reason=draft.reason,
            proposed_content=draft.proposed_content,
            title=draft.title,
            target_source_id=draft.target_source_id,
            target_source_path=draft.target_source_path,
            base_revision=draft.base_revision,
            source_suggestion_id=create.source_suggestion_id,
            created_at=datetime.now(UTC),
        )


class FakeSummarizer:
    def __init__(self, callback=None) -> None:
        self.calls: list[tuple[str | None, tuple[Message, ...]]] = []
        self.callback = callback

    def summarize(self, previous_summary: str | None, messages: tuple[Message, ...]) -> str:
        self.calls.append((previous_summary, messages))
        if self.callback:
            self.callback()
        return " | ".join(
            filter(None, (previous_summary, *(message.content for message in messages)))
        )


class FakeChatModel:
    def __init__(self, response: str = "Fake assistant response") -> None:
        self.response = response
        self.contexts: list[ConversationContext] = []

    def generate(self, context: ConversationContext) -> str:
        self.contexts.append(context)
        return self.response


class FakeClassifier:
    def __init__(self, intent: ConversationIntent = ConversationIntent.CHAT) -> None:
        self.intent = intent
        self.calls: list[tuple[Message, tuple[Message, ...]]] = []

    def classify(
        self,
        current_user_message: Message,
        recent_messages: tuple[Message, ...],
    ) -> ConversationIntent:
        self.calls.append((current_user_message, recent_messages))
        return self.intent


class FakeProposalGenerator:
    def __init__(self) -> None:
        self.contexts: list[ProposalGenerationContext] = []

    def generate(self, context: ProposalGenerationContext) -> ProposalDraft:
        self.contexts.append(context)
        return ProposalDraft(
            title="Test proposal",
            summary="Summary",
            reason="Reason",
            proposed_content="Proposed content",
            target_source_id="knowledge-git",
            target_source_path="economics/test.md",
        )


class FakeKnowledgeRetriever:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.results: list[KnowledgeResult] = [
            FakeKnowledgeResult(
                chunk_key="chunk-1",
                source_id="knowledge-git",
                source_path="economics/fiscal-dominance.md",
                heading_path="Fiskalische Dominanz > These",
                content="Relevant knowledge",
                score=0.9,
                retrieval_mode="quality",
            )
        ]

    def search(self, query: str, mode: str = "quality", limit: int = 5):
        self.calls.append((query, mode, limit))
        return list(self.results)
