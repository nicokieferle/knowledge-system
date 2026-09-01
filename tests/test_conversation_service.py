from __future__ import annotations

import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest

from knowledge_system.conversation_memory import (
    ConversationContextBuilder,
    ConversationMemory,
    ConversationMemoryPolicy,
)
from knowledge_system.conversation_models import (
    ConversationAction,
    ConversationIntent,
    MessageRole,
    ProposalTriggerType,
    SuggestionStatus,
)
from knowledge_system.conversation_service import ConversationService
from knowledge_system.proposal_service import ProposalService
from knowledge_system.proposal_store import (
    ProposalSuggestionNotFoundError,
    ProposalSuggestionStateError,
)
from tests.conversation_fakes import (
    ConversationState,
    FakeChatModel,
    FakeClassifier,
    FakeConversationStore,
    FakeKnowledgeRetriever,
    FakeProposalGenerator,
    FakeProposalStore,
    FakeSummarizer,
    ProposalState,
)


def _service(
    *,
    conversation_state: ConversationState | None = None,
    proposal_state: ProposalState | None = None,
    intent: ConversationIntent = ConversationIntent.CHAT,
    policy: ConversationMemoryPolicy | None = None,
):
    conversation_store = FakeConversationStore(conversation_state)
    proposal_store = FakeProposalStore(proposal_state)
    summarizer = FakeSummarizer()
    memory = ConversationMemory(conversation_store, summarizer, policy)
    classifier = FakeClassifier(intent)
    chat_model = FakeChatModel()
    generator = FakeProposalGenerator()
    retriever = FakeKnowledgeRetriever()
    proposal_service = ProposalService(conversation_store, proposal_store, generator)
    service = ConversationService(
        conversation_store=conversation_store,
        context_builder=ConversationContextBuilder(memory),
        proposal_service=proposal_service,
        chat_model=chat_model,
        intent_classifier=classifier,
        knowledge_retriever=retriever,
    )
    return (
        service,
        conversation_store,
        proposal_store,
        classifier,
        chat_model,
        generator,
        retriever,
        summarizer,
    )


def test_chat_uses_retrieval_and_persists_assistant_with_current_message_once() -> None:
    service, store, proposals, _, chat, _, retriever, _ = _service()
    conversation = service.create_conversation("web")

    result = service.handle_user_message(conversation.id, "Was ist fiskalische Dominanz?")

    assert result.action is ConversationAction.CHAT
    assert result.assistant_message is not None
    assert result.assistant_message.role is MessageRole.ASSISTANT
    assert len(proposals.state.proposals) == 0
    assert retriever.calls == [("Was ist fiskalische Dominanz?", "quality", 5)]
    context = chat.contexts[0]
    assert context.relevant_knowledge[0].source_path.endswith("fiscal-dominance.md")
    assert context.current_user_message.id == result.user_message_id
    assert all(message.id != result.user_message_id for message in context.recent_messages)
    assert [message.role for message in store.list_messages(conversation.id)] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
    ]


@pytest.mark.parametrize("command", ["/remember", "/propose"])
def test_explicit_command_creates_one_pending_proposal_without_classifier(command: str) -> None:
    service, _, proposals, classifier, _, generator, _, _ = _service()
    conversation = service.create_conversation("api")
    service.handle_user_message(conversation.id, "Prior insight")

    first = service.handle_user_message(
        conversation.id,
        command,
        external_message_id="command-1",
    )
    duplicate = service.handle_user_message(
        conversation.id,
        command,
        external_message_id="command-1",
    )

    assert first.action is ConversationAction.PROPOSAL_CREATED
    assert duplicate.proposal_id == first.proposal_id
    assert len(proposals.state.proposals) == 1
    assert len(classifier.calls) == 1  # Only the prior normal chat was classified.
    proposal = proposals.state.proposals[first.proposal_id]
    assert proposal.status == "pending"
    assert proposal.trigger_type is ProposalTriggerType.COMMAND
    assert proposal.target_source_path == "economics/test.md"
    generation_context = generator.contexts[-1]
    assert all(message.content != command for message in generation_context.originating_messages)
    assert generation_context.trigger_message_id not in {
        message.id for message in generation_context.originating_messages
    }


def test_semantic_create_proposal_and_uncertain_are_safe() -> None:
    service, _, proposals, _, _, _, _, _ = _service(intent=ConversationIntent.CREATE_PROPOSAL)
    conversation = service.create_conversation("internal")

    created = service.handle_user_message(conversation.id, "Das bitte aufnehmen.")

    assert created.action is ConversationAction.PROPOSAL_CREATED
    assert proposals.state.proposals[created.proposal_id].trigger_type is ProposalTriggerType.INTENT

    uncertain_service, _, uncertain_proposals, _, _, _, _, _ = _service(
        intent=ConversationIntent.UNCERTAIN
    )
    uncertain_conversation = uncertain_service.create_conversation("internal")
    uncertain = uncertain_service.handle_user_message(uncertain_conversation.id, "Maybe")

    assert uncertain.action is ConversationAction.CHAT
    assert uncertain_proposals.state.proposals == {}


def test_suggestion_persists_across_restart_and_confirmation_is_idempotent() -> None:
    conversation_state = ConversationState()
    proposal_state = ProposalState()
    service, store, proposals, _, _, _, _, _ = _service(
        conversation_state=conversation_state,
        proposal_state=proposal_state,
        intent=ConversationIntent.SUGGEST_PROPOSAL,
    )
    conversation = service.create_conversation("api")

    suggested = service.handle_user_message(
        conversation.id,
        "Der Zins-Schulden-Feedbackkanal ist wichtig.",
    )

    assert suggested.action is ConversationAction.PROPOSAL_CONFIRMATION_REQUIRED
    assert proposals.state.proposals == {}
    suggestion = proposals.get_suggestion(conversation.id, suggested.pending_action_id)
    assert suggestion.status is SuggestionStatus.PENDING
    assert suggested.assistant_message is not None

    restarted, _, restarted_proposals, _, _, generator, _, _ = _service(
        conversation_state=conversation_state,
        proposal_state=proposal_state,
    )
    assert restarted_proposals.get_suggestion(
        conversation.id, suggested.pending_action_id
    ).status is SuggestionStatus.PENDING

    confirmed = restarted.confirm_suggestion(
        conversation.id,
        suggested.pending_action_id,
        "Ja, speicher das.",
        external_message_id="confirmation-1",
    )
    duplicate = restarted.confirm_suggestion(
        conversation.id,
        suggested.pending_action_id,
        "Ja, speicher das.",
        external_message_id="confirmation-1",
    )

    assert confirmed.proposal_id == duplicate.proposal_id
    assert len(restarted_proposals.state.proposals) == 1
    proposal = restarted_proposals.state.proposals[confirmed.proposal_id]
    assert proposal.source_suggestion_id == suggested.pending_action_id
    assert proposal.status == "pending"
    assert restarted_proposals.get_suggestion(
        conversation.id, suggested.pending_action_id
    ).status is SuggestionStatus.CONFIRMED
    confirmation = store.get_message(conversation.id, confirmed.user_message_id)
    assert confirmation.metadata["control"] == "confirm"
    generation_context = generator.contexts[0]
    assert generation_context.trigger_message_id == confirmation.id
    assert all(message.id != confirmation.id for message in generation_context.originating_messages)
    assert all(
        message.content != "Ja, speicher das."
        for message in generation_context.originating_messages
    )


def test_confirmation_failure_rolls_back_and_retry_creates_exactly_one_proposal() -> None:
    service, _, proposals, _, _, _, _, _ = _service(
        intent=ConversationIntent.SUGGEST_PROPOSAL
    )
    conversation = service.create_conversation("api")
    suggested = service.handle_user_message(conversation.id, "Potential insight")
    proposals.fail_confirmation_once = True

    with pytest.raises(RuntimeError, match="simulated failure"):
        service.confirm_suggestion(
            conversation.id,
            suggested.pending_action_id,
            "Yes",
            external_message_id="confirm-1",
        )

    assert proposals.state.proposals == {}
    assert proposals.get_suggestion(
        conversation.id, suggested.pending_action_id
    ).status is SuggestionStatus.PENDING

    retried = service.confirm_suggestion(
        conversation.id,
        suggested.pending_action_id,
        "Yes",
        external_message_id="confirm-1",
    )

    assert retried.proposal_id is not None
    assert len(proposals.state.proposals) == 1


def test_rejection_creates_no_proposal_and_cross_conversation_resolution_is_rejected() -> None:
    service, store, proposals, _, _, _, _, _ = _service(
        intent=ConversationIntent.SUGGEST_PROPOSAL
    )
    first = service.create_conversation("internal")
    second = service.create_conversation("internal")
    suggested = service.handle_user_message(first.id, "Potential insight")

    with pytest.raises(ProposalSuggestionNotFoundError):
        service.confirm_suggestion(second.id, suggested.pending_action_id, "Yes")
    assert store.list_messages(second.id) == []

    rejected = service.reject_suggestion(first.id, suggested.pending_action_id, "No")

    assert rejected.action is ConversationAction.SUGGESTION_REJECTED
    assert proposals.state.proposals == {}
    assert proposals.get_suggestion(
        first.id, suggested.pending_action_id
    ).status is SuggestionStatus.REJECTED


def test_one_confirmation_message_cannot_confirm_two_suggestions() -> None:
    service, _, proposals, classifier, _, generator, _, _ = _service(
        intent=ConversationIntent.SUGGEST_PROPOSAL
    )
    conversation = service.create_conversation("api")
    first = service.handle_user_message(conversation.id, "First possible insight")
    second = service.handle_user_message(conversation.id, "Second possible insight")

    service.confirm_suggestion(
        conversation.id,
        first.pending_action_id,
        "Yes",
        external_message_id="same-confirmation",
    )
    classifier.intent = ConversationIntent.CHAT
    with pytest.raises(ProposalSuggestionStateError):
        service.confirm_suggestion(
            conversation.id,
            second.pending_action_id,
            "Yes",
            external_message_id="same-confirmation",
        )

    assert len(proposals.state.proposals) == 1
    assert len(generator.contexts) == 1
    assert proposals.get_suggestion(
        conversation.id, second.pending_action_id
    ).status is SuggestionStatus.PENDING


def test_conversation_service_import_does_not_load_expensive_retrieval_stack() -> None:
    code = """
import sys
import knowledge_system.conversation_service

assert "sentence_transformers" not in sys.modules
assert "torch" not in sys.modules
assert "knowledge_system.reranker" not in sys.modules
assert "knowledge_system.embedder" not in sys.modules
"""
    subprocess.run([sys.executable, "-c", code], check=True)


def test_v30_fake_demo_keeps_knowledge_unchanged() -> None:
    knowledge_root = Path(__file__).parents[1] / "knowledge"
    before = {
        path.relative_to(knowledge_root): sha256(path.read_bytes()).hexdigest()
        for path in knowledge_root.rglob("*.md")
    }
    service, store, proposals, classifier, _, _, _, summarizer = _service(
        policy=ConversationMemoryPolicy(recent_message_limit=2, summary_trigger_threshold=4)
    )

    economics = service.create_conversation("internal", title="Economics")
    service.handle_user_message(economics.id, "Ich möchte über Fiscal Dominance sprechen.")
    service.handle_user_message(economics.id, "Mein Problem mit dem Argument ist der Zinskanal.")
    service.handle_user_message(economics.id, "Wie hängt das mit Refinanzierung zusammen?")
    service.handle_user_message(economics.id, "Welche Gegenargumente gibt es?")
    assert summarizer.calls
    assert store.get_summary(economics.id) is not None
    original_count = len(store.list_messages(economics.id))

    remembered = service.handle_user_message(economics.id, "/remember")
    assert remembered.action is ConversationAction.PROPOSAL_CREATED
    assert len(store.list_messages(economics.id)) == original_count + 1

    classifier.intent = ConversationIntent.SUGGEST_PROPOSAL
    second = service.create_conversation("internal", title="Second topic")
    suggested = service.handle_user_message(second.id, "Hier ist eine dauerhafte Erkenntnis.")
    assert proposals.state.proposals.get(suggested.pending_action_id) is None
    confirmed = service.confirm_suggestion(second.id, suggested.pending_action_id, "Ja.")
    duplicate = service.confirm_suggestion(second.id, suggested.pending_action_id, "Ja.")
    assert confirmed.proposal_id == duplicate.proposal_id
    assert len(proposals.state.proposals) == 2

    after = {
        path.relative_to(knowledge_root): sha256(path.read_bytes()).hexdigest()
        for path in knowledge_root.rglob("*.md")
    }
    assert after == before
    print(
        f"demo conversations=2 messages={sum(map(len, store.state.messages.values()))} "
        f"summaries={len(store.state.summaries)} proposals={len(proposals.state.proposals)} "
        "knowledge_unchanged=true"
    )
