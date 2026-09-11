from __future__ import annotations

import hashlib
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Barrier
from urllib.parse import urlsplit

from knowledge_system.client_state import ClientIdentity, PostgresClientStateStore
from knowledge_system.config import Settings
from knowledge_system.conversation_memory import (
    ConversationContextBuilder,
    ConversationMemory,
    ConversationMemoryPolicy,
)
from knowledge_system.conversation_models import (
    ConversationContext,
    ConversationIntent,
    Message,
    MessageRole,
    ProposalDraft,
    ProposalGenerationContext,
    SuggestionStatus,
)
from knowledge_system.conversation_service import ConversationService
from knowledge_system.conversation_store import PostgresConversationStore
from knowledge_system.db import connect, init_db
from knowledge_system.durable_state import DURABLE_TABLES, read_durable_state_fingerprint
from knowledge_system.proposal_service import ProposalService
from knowledge_system.proposal_store import PostgresProposalStore

SMOKE_CONFIRMATION = "isolated-v30-smoke-database"


@dataclass(frozen=True)
class FakeKnowledgeResult:
    chunk_key: str = "smoke-chunk"
    source_id: str = "smoke-source"
    source_path: str = "smoke/document.md"
    heading_path: str = "Smoke"
    content: str = "Synthetic retrieval context"
    score: float = 1.0
    retrieval_mode: str = "quality"


class FakeKnowledgeRetriever:
    def search(self, query: str, mode: str = "quality", limit: int = 5):
        return [FakeKnowledgeResult()]


class FakeChatModel:
    def generate(self, context: ConversationContext) -> str:
        return "Synthetic assistant response"


class FakeIntentClassifier:
    def __init__(self, intent: ConversationIntent = ConversationIntent.CHAT) -> None:
        self.intent = intent

    def classify(
        self,
        current_user_message: Message,
        recent_messages: tuple[Message, ...],
    ) -> ConversationIntent:
        return self.intent


class FakeSummarizer:
    def summarize(self, previous_summary: str | None, messages: tuple[Message, ...]) -> str:
        return f"Synthetic summary through {messages[-1].id}"


class BarrierSummarizer:
    def __init__(self, barrier: Barrier, label: str) -> None:
        self.barrier = barrier
        self.label = label

    def summarize(self, previous_summary: str | None, messages: tuple[Message, ...]) -> str:
        self.barrier.wait(timeout=10)
        return f"Concurrent summary {self.label} through {messages[-1].id}"


class FakeProposalGenerator:
    def generate(self, context: ProposalGenerationContext) -> ProposalDraft:
        return ProposalDraft(
            title="Synthetic proposal",
            summary="Synthetic proposal summary",
            reason="PostgreSQL smoke verification",
            proposed_content="Synthetic proposed content",
            target_source_id="smoke-source",
            target_source_path="smoke/document.md",
        )


def _build_service(
    settings: Settings,
    classifier: FakeIntentClassifier,
    summarizer=None,
) -> ConversationService:
    conversation_store = PostgresConversationStore(settings)
    proposal_store = PostgresProposalStore(settings)
    memory = ConversationMemory(
        conversation_store,
        summarizer or FakeSummarizer(),
        ConversationMemoryPolicy(recent_message_limit=2, summary_trigger_threshold=4),
    )
    return ConversationService(
        conversation_store=conversation_store,
        context_builder=ConversationContextBuilder(memory),
        proposal_service=ProposalService(
            conversation_store,
            proposal_store,
            FakeProposalGenerator(),
        ),
        chat_model=FakeChatModel(),
        intent_classifier=classifier,
        knowledge_retriever=FakeKnowledgeRetriever(),
    )


def _knowledge_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _durable_counts(settings: Settings) -> dict[str, int]:
    with connect(settings) as conn:
        return {
            table: int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in DURABLE_TABLES
        }


def _assert_isolated_target(database_url: str) -> None:
    database_name = urlsplit(database_url).path.lstrip("/").lower()
    if not database_name or not any(marker in database_name for marker in ("smoke", "test")):
        raise RuntimeError("TEST_DATABASE_URL must name an isolated smoke/test database")
    if os.getenv("V30_SMOKE_CONFIRM") != SMOKE_CONFIRMATION:
        raise RuntimeError("V30_SMOKE_CONFIRM safety acknowledgement is missing")


def run_smoke(settings: Settings) -> dict[str, object]:
    knowledge_before = _knowledge_hash(settings.knowledge_root)
    init_db(settings)

    with connect(settings) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                """
                SELECT tablename FROM pg_tables
                WHERE schemaname = 'public' AND tablename = ANY(%s)
                """,
                (list(DURABLE_TABLES),),
            ).fetchall()
        }
    assert tables == set(DURABLE_TABLES)
    assert all(count == 0 for count in _durable_counts(settings).values())

    classifier = FakeIntentClassifier()
    service = _build_service(settings, classifier)
    conversation_a = service.create_conversation("smoke", title="Synthetic A")
    for index in range(4):
        service.handle_user_message(
            conversation_a.id,
            f"Synthetic conversation A message {index}",
        )

    reopened_store = PostgresConversationStore(settings)
    reopened_store.get_conversation(conversation_a.id)
    messages_a = reopened_store.list_messages(conversation_a.id)
    assert len(messages_a) == 8
    assert [message.id for message in messages_a] == sorted(message.id for message in messages_a)
    summary_a = reopened_store.get_summary(conversation_a.id)
    assert summary_a is not None
    assert summary_a.through_message_id == messages_a[3].id
    assert len(reopened_store.list_messages(conversation_a.id)) == 8

    conversation_b = service.create_conversation("smoke", title="Synthetic B")
    service.handle_user_message(conversation_b.id, "Synthetic context for B")
    assert all(
        message.conversation_id == conversation_b.id
        for message in reopened_store.list_messages(conversation_b.id)
    )

    client_identity = ClientIdentity(
        client_type="telegram",
        external_chat_id="synthetic-chat-1",
        external_user_id="synthetic-user-1",
    )
    client_states = PostgresClientStateStore(settings)
    assert client_states.get(client_identity) is None
    first_state = client_states.activate(client_identity, conversation_a.id)
    second_state = client_states.activate(client_identity, conversation_b.id)
    assert first_state.version == 1
    assert second_state.version == 2
    assert second_state.active_conversation_id == conversation_b.id
    assert [
        conversation.id for conversation in client_states.list_conversations(client_identity)
    ] == [
        conversation_b.id,
        conversation_a.id,
    ]
    bound_first = client_states.bind_message(
        client_identity,
        "synthetic-telegram-message-1",
        conversation_a.id,
    )
    bound_retry = client_states.bind_message(
        client_identity,
        "synthetic-telegram-message-1",
        conversation_b.id,
    )
    assert bound_first == bound_retry == conversation_a.id

    command_result = service.handle_user_message(conversation_a.id, "/remember")
    classifier.intent = ConversationIntent.CREATE_PROPOSAL
    intent_result = service.handle_user_message(conversation_b.id, "Synthetic create trigger")
    assert command_result.proposal_id != intent_result.proposal_id
    assert _durable_counts(settings)["proposals"] == 2

    classifier.intent = ConversationIntent.SUGGEST_PROPOSAL
    suggested = service.handle_user_message(conversation_b.id, "Synthetic suggested insight")
    assert _durable_counts(settings)["proposals"] == 2

    del service
    restarted_classifier = FakeIntentClassifier()
    restarted = _build_service(settings, restarted_classifier)
    restarted_suggestion = restarted.proposal_service.get_suggestion(
        conversation_b.id,
        suggested.pending_action_id,
    )
    assert restarted_suggestion.status is SuggestionStatus.PENDING

    confirmed = restarted.confirm_suggestion(
        conversation_b.id,
        suggested.pending_action_id,
        "Synthetic confirmation",
        external_message_id="smoke-confirmation-1",
    )
    duplicate = restarted.confirm_suggestion(
        conversation_b.id,
        suggested.pending_action_id,
        "Synthetic confirmation",
        external_message_id="smoke-confirmation-1",
    )
    assert confirmed.proposal_id == duplicate.proposal_id
    with connect(settings) as conn:
        linked = conn.execute(
            "SELECT id, source_suggestion_id FROM proposals WHERE source_suggestion_id = %s",
            (suggested.pending_action_id,),
        ).fetchall()
    assert linked == [(confirmed.proposal_id, suggested.pending_action_id)]

    restarted_classifier.intent = ConversationIntent.SUGGEST_PROPOSAL
    concurrent_suggestion = restarted.handle_user_message(
        conversation_b.id,
        "Synthetic concurrent suggestion",
    )
    barrier = Barrier(2)

    def confirm_concurrently(label: str):
        concurrent_service = _build_service(settings, FakeIntentClassifier())
        barrier.wait(timeout=10)
        return concurrent_service.confirm_suggestion(
            conversation_b.id,
            concurrent_suggestion.pending_action_id,
            f"Synthetic concurrent confirmation {label}",
            external_message_id=f"concurrent-confirmation-{label}",
        ).proposal_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        proposal_ids = list(executor.map(confirm_concurrently, ("a", "b")))
    assert proposal_ids[0] == proposal_ids[1]
    with connect(settings) as conn:
        concurrent_count = conn.execute(
            "SELECT count(*) FROM proposals WHERE source_suggestion_id = %s",
            (concurrent_suggestion.pending_action_id,),
        ).fetchone()[0]
    assert concurrent_count == 1

    conversation_c = restarted.create_conversation("smoke", title="Synthetic C")
    for index in range(6):
        reopened_store.add_message(
            conversation_c.id,
            MessageRole.USER,
            f"Synthetic summary source {index}",
        )
    current_c = reopened_store.add_message(
        conversation_c.id,
        MessageRole.USER,
        "Synthetic summary boundary marker",
    )
    summary_barrier = Barrier(2)

    def summarize_concurrently(label: str):
        memory = ConversationMemory(
            PostgresConversationStore(settings),
            BarrierSummarizer(summary_barrier, label),
            ConversationMemoryPolicy(recent_message_limit=2, summary_trigger_threshold=4),
        )
        return memory.load_history(conversation_c.id, before_message_id=current_c.id).summary

    with ThreadPoolExecutor(max_workers=2) as executor:
        summaries = list(executor.map(summarize_concurrently, ("a", "b")))
    summary_c = PostgresConversationStore(settings).get_summary(conversation_c.id)
    assert summary_c is not None
    assert summary_c.version == 1
    assert summary_c.through_message_id == reopened_store.list_messages(conversation_c.id)[3].id
    assert summaries == [summary_c.summary, summary_c.summary]

    final_store = PostgresConversationStore(settings)
    final_store.get_conversation(conversation_a.id)
    final_store.get_conversation(conversation_b.id)
    final_store.get_conversation(conversation_c.id)
    final_counts = _durable_counts(settings)
    assert final_counts == {
        "conversations": 3,
        "client_states": 1,
        "client_conversations": 2,
        "client_message_bindings": 1,
        "messages": 26,
        "conversation_summaries": 3,
        "proposal_suggestions": 2,
        "proposals": 4,
        "proposal_reviews": 0,
        "proposal_decisions": 0,
    }
    final_fingerprint = read_durable_state_fingerprint(settings)
    assert final_fingerprint.counts == final_counts
    assert _knowledge_hash(settings.knowledge_root) == knowledge_before

    return {
        "schema_initialized": True,
        "conversations_persisted_after_reconnect": True,
        "messages_persisted": True,
        "summary_persisted": True,
        "summary_boundary_correct": True,
        "pending_suggestion_persisted_across_restart": True,
        "confirmation_atomic": True,
        "duplicate_confirmation_idempotent": True,
        "concurrent_confirmation_tested": True,
        "concurrent_summary_tested": True,
        "client_state_persisted": True,
        "client_message_binding_idempotent": True,
        "knowledge_unchanged": True,
        "durable_state_counts": final_counts,
        "durable_state_sha256": final_fingerprint.sha256,
    }


def main() -> int:
    database_url = os.getenv("TEST_DATABASE_URL", "").strip()
    if not database_url:
        print("TEST_DATABASE_URL is required", file=sys.stderr)
        return 2

    try:
        _assert_isolated_target(database_url)
        settings = Settings(
            database_url=database_url,
            knowledge_root=Path(os.getenv("KNOWLEDGE_ROOT", "knowledge")).resolve(),
            embedding_model="unused-for-smoke",
            embedding_dimensions=int(os.getenv("EMBEDDING_DIMENSIONS", "384")),
        )
        result = run_smoke(settings)
    except Exception as exc:  # noqa: BLE001 - redact all unexpected smoke failures.
        print(f"conversation_postgres_smoke_error={type(exc).__name__}", file=sys.stderr)
        return 1

    for key, value in result.items():
        if isinstance(value, dict):
            value = ",".join(f"{name}:{count}" for name, count in sorted(value.items()))
        elif isinstance(value, bool):
            value = str(value).lower()
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
