from __future__ import annotations

from knowledge_system.conversation_memory import (
    ConversationContextBuilder,
    ConversationMemory,
    ConversationMemoryPolicy,
)
from knowledge_system.conversation_models import MessageRole
from tests.conversation_fakes import FakeConversationStore, FakeSummarizer


def test_short_conversation_remains_fully_available_without_summary() -> None:
    store = FakeConversationStore()
    conversation = store.create_conversation("internal")
    first = store.add_message(conversation.id, MessageRole.USER, "One")
    second = store.add_message(conversation.id, MessageRole.ASSISTANT, "Two")
    current = store.add_message(conversation.id, MessageRole.USER, "Current")
    summarizer = FakeSummarizer()
    memory = ConversationMemory(
        store,
        summarizer,
        ConversationMemoryPolicy(recent_message_limit=2, summary_trigger_threshold=4),
    )

    history = memory.load_history(conversation.id, before_message_id=current.id)

    assert history.summary is None
    assert history.recent_messages == (first, second)
    assert summarizer.calls == []


def test_summary_uses_immutable_snapshot_and_exact_boundary() -> None:
    store = FakeConversationStore()
    conversation = store.create_conversation("internal")
    originals = [
        store.add_message(conversation.id, MessageRole.USER, f"Message {index}")
        for index in range(1, 7)
    ]
    current = store.add_message(conversation.id, MessageRole.USER, "Current")
    summarizer = FakeSummarizer(
        callback=lambda: store.add_message(conversation.id, MessageRole.USER, "Arrived later")
    )
    memory = ConversationMemory(
        store,
        summarizer,
        ConversationMemoryPolicy(recent_message_limit=2, summary_trigger_threshold=4),
    )

    history = memory.load_history(conversation.id, before_message_id=current.id)
    summary = store.get_summary(conversation.id)

    assert tuple(message.id for message in summarizer.calls[0][1]) == tuple(
        message.id for message in originals[:4]
    )
    assert summary is not None
    assert summary.through_message_id == originals[3].id
    assert history.recent_messages == tuple(originals[4:])
    assert len(store.list_messages(conversation.id)) == 8


def test_summary_cas_keeps_concurrent_winner() -> None:
    store = FakeConversationStore()
    conversation = store.create_conversation("internal")
    messages = [
        store.add_message(conversation.id, MessageRole.USER, f"Message {index}")
        for index in range(6)
    ]
    current = store.add_message(conversation.id, MessageRole.USER, "Current")

    def save_concurrent_winner() -> None:
        assert store.compare_and_set_summary(
            conversation.id,
            expected_version=0,
            summary="Concurrent winner",
            through_message_id=messages[3].id,
        )

    memory = ConversationMemory(
        store,
        FakeSummarizer(callback=save_concurrent_winner),
        ConversationMemoryPolicy(recent_message_limit=2, summary_trigger_threshold=4),
    )

    history = memory.load_history(conversation.id, before_message_id=current.id)

    assert history.summary == "Concurrent winner"
    assert store.get_summary(conversation.id).version == 1  # type: ignore[union-attr]


def test_existing_summary_is_not_resummarized_and_conversations_are_isolated() -> None:
    store = FakeConversationStore()
    first = store.create_conversation("internal")
    second = store.create_conversation("internal")
    for index in range(6):
        store.add_message(first.id, MessageRole.USER, f"A{index}")
    first_current = store.add_message(first.id, MessageRole.USER, "A current")
    second_message = store.add_message(second.id, MessageRole.USER, "B only")
    second_current = store.add_message(second.id, MessageRole.USER, "B current")
    summarizer = FakeSummarizer()
    memory = ConversationMemory(
        store,
        summarizer,
        ConversationMemoryPolicy(recent_message_limit=2, summary_trigger_threshold=4),
    )

    memory.load_history(first.id, before_message_id=first_current.id)
    initial_call_ids = tuple(message.id for message in summarizer.calls[0][1])
    memory.load_history(first.id, before_message_id=first_current.id)
    second_history = memory.load_history(second.id, before_message_id=second_current.id)

    assert len(summarizer.calls) == 1
    assert initial_call_ids
    assert second_history.summary is None
    assert second_history.recent_messages == (second_message,)


def test_next_summary_only_receives_messages_after_previous_boundary() -> None:
    store = FakeConversationStore()
    conversation = store.create_conversation("internal")
    for index in range(6):
        store.add_message(conversation.id, MessageRole.USER, f"Initial {index}")
    first_current = store.add_message(conversation.id, MessageRole.USER, "First current")
    summarizer = FakeSummarizer()
    memory = ConversationMemory(
        store,
        summarizer,
        ConversationMemoryPolicy(recent_message_limit=2, summary_trigger_threshold=4),
    )
    memory.load_history(conversation.id, before_message_id=first_current.id)
    first_summary = store.get_summary(conversation.id)
    assert first_summary is not None

    for index in range(3):
        store.add_message(conversation.id, MessageRole.ASSISTANT, f"Later {index}")
    second_current = store.add_message(conversation.id, MessageRole.USER, "Second current")
    memory.load_history(conversation.id, before_message_id=second_current.id)

    assert len(summarizer.calls) == 2
    previous_summary, second_batch = summarizer.calls[1]
    assert previous_summary == first_summary.summary
    assert all(message.id > first_summary.through_message_id for message in second_batch)


def test_context_builder_excludes_current_user_message() -> None:
    store = FakeConversationStore()
    conversation = store.create_conversation("internal")
    prior = store.add_message(conversation.id, MessageRole.USER, "Prior")
    current = store.add_message(conversation.id, MessageRole.USER, "Current")
    memory = ConversationMemory(store, FakeSummarizer())

    context = ConversationContextBuilder(memory).build(conversation.id, current, ())

    assert context.recent_messages == (prior,)
    assert context.current_user_message == current
    assert sum(
        message.id == current.id
        for message in (*context.recent_messages, context.current_user_message)
    ) == 1
