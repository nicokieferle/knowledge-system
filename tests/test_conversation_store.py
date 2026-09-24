from __future__ import annotations

from knowledge_system.conversation_models import MessageRole
from tests.conversation_fakes import ConversationState, FakeConversationStore


def test_conversations_messages_order_and_external_ids_persist_across_store_instances() -> None:
    state = ConversationState()
    first_store = FakeConversationStore(state)
    conversation = first_store.create_conversation(
        "api",
        title="Economics",
        external_conversation_id="thread-42",
    )
    first = first_store.add_message(
        conversation.id,
        MessageRole.USER,
        "First",
        external_message_id="message-1",
    )
    second = first_store.add_message(
        conversation.id,
        MessageRole.ASSISTANT,
        "Second",
    )

    reopened_store = FakeConversationStore(state)
    reopened = reopened_store.get_conversation(conversation.id)
    messages = reopened_store.list_messages(conversation.id)

    assert reopened.client_type == "api"
    assert reopened.external_conversation_id == "thread-42"
    assert messages == [first, second]
    assert [message.role for message in messages] == [MessageRole.USER, MessageRole.ASSISTANT]


def test_messages_are_isolated_by_conversation() -> None:
    store = FakeConversationStore()
    first = store.create_conversation("internal")
    second = store.create_conversation("web")
    store.add_message(first.id, MessageRole.USER, "Only first")
    store.add_message(second.id, MessageRole.USER, "Only second")

    assert [message.content for message in store.list_messages(first.id)] == ["Only first"]
    assert [message.content for message in store.list_messages(second.id)] == ["Only second"]


def test_duplicate_external_message_id_is_idempotent() -> None:
    store = FakeConversationStore()
    conversation = store.create_conversation("api")

    first = store.add_message(
        conversation.id,
        MessageRole.USER,
        "Original",
        external_message_id="external-1",
    )
    duplicate = store.add_message(
        conversation.id,
        MessageRole.USER,
        "Duplicate delivery",
        external_message_id="external-1",
    )

    assert duplicate == first
    assert len(store.list_messages(conversation.id)) == 1
