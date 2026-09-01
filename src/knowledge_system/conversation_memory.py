from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from .conversation_models import (
    ConversationContext,
    ConversationSummarizer,
    ConversationSummary,
    KnowledgeResult,
    Message,
)
from .conversation_store import ConversationStore


@dataclass(frozen=True)
class ConversationMemoryPolicy:
    recent_message_limit: int = 12
    summary_trigger_threshold: int = 30

    def __post_init__(self) -> None:
        if self.recent_message_limit < 1:
            raise ValueError("recent_message_limit must be at least 1")
        if self.summary_trigger_threshold <= self.recent_message_limit:
            raise ValueError("summary_trigger_threshold must exceed recent_message_limit")


@dataclass(frozen=True)
class ConversationHistoryContext:
    summary: str | None
    recent_messages: tuple[Message, ...]


class ConversationMemory:
    def __init__(
        self,
        store: ConversationStore,
        summarizer: ConversationSummarizer,
        policy: ConversationMemoryPolicy | None = None,
    ) -> None:
        self.store = store
        self.summarizer = summarizer
        self.policy = policy or ConversationMemoryPolicy()

    def load_history(
        self,
        conversation_id: UUID,
        *,
        before_message_id: int,
    ) -> ConversationHistoryContext:
        summary_state = self._update_summary_if_needed(
            conversation_id,
            before_message_id=before_message_id,
        )
        if summary_state is not None and summary_state.through_message_id >= before_message_id:
            # A later worker may have advanced the shared rolling summary past this
            # request's immutable boundary. Raw history is the only safe context for
            # this older snapshot because prior summary versions are not retained.
            summary_state = None
        through_message_id = summary_state.through_message_id if summary_state else None
        recent = self.store.list_messages(
            conversation_id,
            after_message_id=through_message_id,
            before_message_id=before_message_id,
        )
        return ConversationHistoryContext(
            summary=summary_state.summary if summary_state else None,
            recent_messages=tuple(recent),
        )

    def _update_summary_if_needed(
        self,
        conversation_id: UUID,
        *,
        before_message_id: int,
    ) -> ConversationSummary | None:
        previous = self.store.get_summary(conversation_id)
        unsummarized = self.store.list_messages(
            conversation_id,
            after_message_id=previous.through_message_id if previous else None,
            before_message_id=before_message_id,
        )
        if len(unsummarized) <= self.policy.summary_trigger_threshold:
            return previous

        batch_size = len(unsummarized) - self.policy.recent_message_limit
        batch = tuple(unsummarized[:batch_size])
        snapshot_boundary = batch[-1].id
        new_summary = self.summarizer.summarize(
            previous.summary if previous else None,
            batch,
        )
        saved = self.store.compare_and_set_summary(
            conversation_id,
            expected_version=previous.version if previous else 0,
            summary=new_summary,
            through_message_id=snapshot_boundary,
        )
        if saved:
            print(
                f"[conversation] Summary updated conversation={conversation_id} "
                f"through_message_id={snapshot_boundary}"
            )
        # Another worker may have won the optimistic update. Always use the
        # persisted winner instead of overwriting it with a stale summary.
        return self.store.get_summary(conversation_id)


class ConversationContextBuilder:
    def __init__(self, memory: ConversationMemory) -> None:
        self.memory = memory

    def build(
        self,
        conversation_id: UUID,
        current_user_message: Message,
        relevant_knowledge: tuple[KnowledgeResult, ...],
        history: ConversationHistoryContext | None = None,
    ) -> ConversationContext:
        history = history or self.memory.load_history(
            conversation_id, before_message_id=current_user_message.id
        )
        return ConversationContext(
            conversation_id=conversation_id,
            conversation_summary=history.summary,
            recent_messages=history.recent_messages,
            relevant_knowledge=relevant_knowledge,
            current_user_message=current_user_message,
        )

    def load_history(
        self,
        conversation_id: UUID,
        *,
        before_message_id: int,
    ) -> ConversationHistoryContext:
        return self.memory.load_history(
            conversation_id,
            before_message_id=before_message_id,
        )
