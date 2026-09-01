from __future__ import annotations

from uuid import UUID

from .conversation_models import (
    KnowledgeResult,
    Message,
    Proposal,
    ProposalGenerationContext,
    ProposalGenerator,
    ProposalSuggestion,
    ProposalTriggerType,
)
from .conversation_store import ConversationStore
from .proposal_store import ProposalCreate, ProposalStore


class ProposalService:
    def __init__(
        self,
        conversation_store: ConversationStore,
        proposal_store: ProposalStore,
        generator: ProposalGenerator,
    ) -> None:
        self.conversation_store = conversation_store
        self.proposal_store = proposal_store
        self.generator = generator

    def create_proposal(
        self,
        *,
        conversation_id: UUID,
        source_client: str,
        trigger_message_id: int,
        trigger_type: ProposalTriggerType,
        conversation_summary: str | None,
        originating_messages: tuple[Message, ...],
        relevant_knowledge: tuple[KnowledgeResult, ...],
    ) -> Proposal:
        generation_context = ProposalGenerationContext(
            conversation_id=conversation_id,
            trigger_message_id=trigger_message_id,
            trigger_type=trigger_type,
            conversation_summary=conversation_summary,
            originating_messages=originating_messages,
            relevant_knowledge=relevant_knowledge,
        )
        draft = self.generator.generate(generation_context)
        return self.proposal_store.create_proposal(
            ProposalCreate(
                conversation_id=conversation_id,
                source_client=source_client,
                trigger_type=trigger_type,
                trigger_message_id=trigger_message_id,
                originating_message_ids=tuple(
                    message.id for message in originating_messages
                ),
                draft=draft,
            )
        )

    def create_suggestion(
        self,
        *,
        conversation_id: UUID,
        trigger_message_id: int,
        conversation_summary: str | None,
        originating_messages: tuple[Message, ...],
    ) -> ProposalSuggestion:
        return self.proposal_store.create_suggestion(
            conversation_id,
            trigger_message_id,
            tuple(message.id for message in originating_messages),
            conversation_summary,
        )

    def get_suggestion(
        self,
        conversation_id: UUID,
        suggestion_id: UUID,
    ) -> ProposalSuggestion:
        return self.proposal_store.get_suggestion(conversation_id, suggestion_id)

    def confirm_suggestion(
        self,
        *,
        conversation_id: UUID,
        suggestion_id: UUID,
        confirmation_message_id: int,
        source_client: str,
        relevant_knowledge: tuple[KnowledgeResult, ...],
    ) -> Proposal:
        suggestion = self.proposal_store.get_suggestion(conversation_id, suggestion_id)
        originating_ids = tuple(
            dict.fromkeys((*suggestion.originating_message_ids, suggestion.trigger_message_id))
        )
        originating_messages = tuple(
            self.conversation_store.get_messages(conversation_id, originating_ids)
        )
        generation_context = ProposalGenerationContext(
            conversation_id=conversation_id,
            trigger_message_id=confirmation_message_id,
            trigger_type=ProposalTriggerType.CONFIRMED_SUGGESTION,
            conversation_summary=suggestion.context_summary,
            originating_messages=originating_messages,
            relevant_knowledge=relevant_knowledge,
        )
        draft = self.generator.generate(generation_context)
        return self.proposal_store.confirm_suggestion(
            conversation_id,
            suggestion_id,
            confirmation_message_id,
            source_client,
            draft,
        )

    def reject_suggestion(
        self,
        *,
        conversation_id: UUID,
        suggestion_id: UUID,
        resolution_message_id: int,
    ) -> ProposalSuggestion:
        return self.proposal_store.reject_suggestion(
            conversation_id,
            suggestion_id,
            resolution_message_id,
        )
