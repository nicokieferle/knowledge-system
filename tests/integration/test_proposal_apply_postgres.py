"""V3.3 apply/index acceptance against isolated PostgreSQL 17."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier, Event, Lock

import psycopg
import pytest

from knowledge_system.apply_schema import APPLY_SCHEMA
from knowledge_system.client_state import ClientIdentity
from knowledge_system.db import init_db
from knowledge_system.indexer import index_document
from knowledge_system.knowledge_apply import SecureKnowledgeWriter
from knowledge_system.proposal_apply import PostgresApplyStore, ProposalApplyService
from knowledge_system.proposal_review import InvalidTransition, ProposalStatus, ReviewForbidden
from knowledge_system.review_schema import REVIEW_SCHEMA
from knowledge_system.sources import GitMarkdownSource
from tests.integration import test_proposal_review_postgres as pg_review
from tests.integration.test_proposal_review_postgres import seed


@pytest.fixture
def database(tmp_path):
    yield from pg_review.database.__wrapped__(tmp_path)


class FakeEmbedder:
    def encode(self, values):
        return [[float(index + 1)] * 384 for index, _ in enumerate(values)]


def initialize(settings):
    init_db(settings)


def document_indexer(settings):
    return lambda document: index_document(settings, document, embedder=FakeEmbedder())


def test_apply_schema_is_atomic_on_autocommit_failure(database):
    with psycopg.connect(database.database_url, autocommit=True) as conn:
        for statement in pg_review.v312_statements():
            conn.execute(statement)
        conn.execute(REVIEW_SCHEMA)
        with pytest.raises(psycopg.Error):
            conn.execute(APPLY_SCHEMA + "\nSELECT definitely_missing_v33_function();")
        assert conn.execute("SELECT to_regclass('proposal_applies')").fetchone()[0] is None


def test_migration_never_creates_apply_for_previously_accepted_proposal(database):
    pg_review.initialize(database)
    reviews, who, proposal = seed(database)
    revision = reviews.prepare(who, proposal.id).revision
    reviews.decide(who, proposal.id, revision.id, ProposalStatus.ACCEPTED)

    with psycopg.connect(database.database_url, autocommit=True) as conn:
        conn.execute(APPLY_SCHEMA)
        assert conn.execute("SELECT count(*) FROM proposal_applies").fetchone()[0] == 0
    assert not (database.knowledge_root / "new.md").exists()

    initialize(database)
    service = ProposalApplyService(database, document_indexer=document_indexer(database))
    result = service.apply_accepted(who, proposal.id, revision.id)
    assert result.apply.apply_status == "applied"
    assert result.apply.index_status == "indexed"
    assert (database.knowledge_root / "new.md").read_text(encoding="utf-8") == "new\n"


def test_acceptance_rolls_back_if_durable_intent_cannot_be_created(database):
    initialize(database)
    reviews, who, proposal = seed(database)
    revision = reviews.prepare(who, proposal.id).revision
    with psycopg.connect(database.database_url, autocommit=True) as conn:
        conn.execute(
            """CREATE FUNCTION inject_intent_failure() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'synthetic intent failure'; END $$"""
        )
        conn.execute(
            """CREATE TRIGGER aaa_inject_intent_failure BEFORE INSERT ON proposal_applies
            FOR EACH ROW EXECUTE FUNCTION inject_intent_failure()"""
        )
    with pytest.raises(psycopg.Error, match="synthetic intent failure"):
        ProposalApplyService(database).accept_and_apply(who, proposal.id, revision.id, "pending")
    assert reviews.get(who, proposal.id).proposal.status == "pending"
    assert not (database.knowledge_root / "new.md").exists()
    with psycopg.connect(database.database_url) as conn:
        assert conn.execute("SELECT count(*) FROM proposal_decisions").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM proposal_applies").fetchone()[0] == 0


def test_accept_apply_writes_exactly_once_and_indexes_idempotently(database):
    initialize(database)
    reviews, who, proposal = seed(database)
    revision = reviews.prepare(who, proposal.id).revision
    calls = 0
    lock = Lock()

    def index(document):
        nonlocal calls
        with lock:
            calls += 1
        return index_document(database, document, embedder=FakeEmbedder())

    service = ProposalApplyService(database, document_indexer=index)
    start = Barrier(2)

    def submit():
        start.wait(timeout=10)
        return service.accept_and_apply(who, proposal.id, revision.id, "pending")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: submit(), range(2)))

    assert all(result.apply.apply_status == "applied" for result in results)
    assert all(result.apply.index_status == "indexed" for result in results)
    assert calls == 1
    assert (database.knowledge_root / "new.md").read_bytes() == b"new\n"
    with psycopg.connect(database.database_url) as conn:
        assert conn.execute("SELECT count(*) FROM proposal_applies").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM proposal_decisions").fetchone()[0] == 1
        assert (
            conn.execute("SELECT count(*) FROM chunks WHERE source_path='new.md'").fetchone()[0]
            == 1
        )


def test_two_proposals_for_same_base_have_one_write_and_one_conflict(database):
    initialize(database)
    target = database.knowledge_root / "shared.md"
    target.write_text("base\n", encoding="utf-8")
    first_reviews, first_who, first = seed(database)
    second_reviews, second_who, second = seed(database)
    first_revision = first_reviews.prepare(
        first_who, first.id, target_source_path="shared.md", new_content="first\n"
    ).revision
    second_revision = second_reviews.prepare(
        second_who, second.id, target_source_path="shared.md", new_content="second\n"
    ).revision
    store = PostgresApplyStore(database)
    store.accept_and_intent(first_who, first.id, first_revision.id, "pending")
    store.accept_and_intent(second_who, second.id, second_revision.id, "pending")
    writer = SecureKnowledgeWriter(database.knowledge_root)
    start = Barrier(2)

    def apply(args):
        identity, proposal_id = args
        start.wait(timeout=10)
        return store.execute_apply(identity, proposal_id, writer).apply.apply_status

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(apply, [(first_who, first.id), (second_who, second.id)]))
    assert sorted(statuses) == ["applied", "conflict"]
    assert target.read_text(encoding="utf-8") in ("first\n", "second\n")


def test_refresh_during_apply_waits_and_cannot_replace_success(database):
    initialize(database)
    reviews, who, proposal = seed(database)
    revision = reviews.prepare(who, proposal.id).revision
    store = PostgresApplyStore(database)
    store.accept_and_intent(who, proposal.id, revision.id, "pending")
    entered = Event()
    release = Event()

    def pause(phase):
        if phase == "after_basis_read":
            entered.set()
            assert release.wait(timeout=10)

    service = ProposalApplyService(
        database,
        store=store,
        writer=SecureKnowledgeWriter(database.knowledge_root, pause),
        document_indexer=document_indexer(database),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        applying = pool.submit(service.retry_apply, who, proposal.id)
        assert entered.wait(timeout=10)
        refreshing = pool.submit(service.refresh_review, who, proposal.id)
        release.set()
        assert applying.result(timeout=10).apply.apply_status == "applied"
        with pytest.raises(InvalidTransition):
            refreshing.result(timeout=10)
    assert store.get(who, proposal.id).apply.refresh_proposal_id is None


class SimulatedProcessLoss(BaseException):
    pass


def test_retry_after_file_write_before_database_completion(database):
    initialize(database)
    reviews, who, proposal = seed(database)
    revision = reviews.prepare(who, proposal.id).revision
    store = PostgresApplyStore(database)
    store.accept_and_intent(who, proposal.id, revision.id, "pending")

    def crash(phase):
        if phase == "after_rename":
            raise SimulatedProcessLoss()

    with pytest.raises(SimulatedProcessLoss):
        store.execute_apply(who, proposal.id, SecureKnowledgeWriter(database.knowledge_root, crash))
    assert (database.knowledge_root / "new.md").read_text(encoding="utf-8") == "new\n"
    assert store.get(who, proposal.id).apply.apply_status == "pending"

    result = store.execute_apply(who, proposal.id, SecureKnowledgeWriter(database.knowledge_root))
    assert result.apply.apply_status == "applied"
    assert not list(database.knowledge_root.glob(".knowledge-apply-*"))


def test_index_failure_keeps_applied_file_and_retry_replaces_chunks(database):
    initialize(database)
    reviews, who, proposal = seed(database)
    revision = reviews.prepare(who, proposal.id).revision
    store = PostgresApplyStore(database)
    store.accept_and_intent(who, proposal.id, revision.id, "pending")
    applied = store.execute_apply(who, proposal.id, SecureKnowledgeWriter(database.knowledge_root))
    assert applied.apply.apply_status == "applied"

    def committed_then_lost_confirmation(document):
        index_document(database, document, embedder=FakeEmbedder())
        raise RuntimeError("private details must not be stored")

    failed = store.execute_index(
        who,
        proposal.id,
        GitMarkdownSource(database.knowledge_root),
        committed_then_lost_confirmation,
    )
    assert failed.apply.apply_status == "applied"
    assert failed.apply.index_status == "failed"
    assert failed.apply.index_error_class == "index_operation_failed"
    assert (database.knowledge_root / "new.md").read_text(encoding="utf-8") == "new\n"

    indexed = store.execute_index(
        who,
        proposal.id,
        GitMarkdownSource(database.knowledge_root),
        document_indexer(database),
    )
    assert indexed.apply.index_status == "indexed"
    with psycopg.connect(database.database_url) as conn:
        assert (
            conn.execute("SELECT count(*) FROM chunks WHERE source_path='new.md'").fetchone()[0]
            == 1
        )


def test_conflict_and_reject_defer_never_write_or_index(database):
    initialize(database)
    reviews, who, proposal = seed(database)
    revision = reviews.prepare(who, proposal.id).revision
    (database.knowledge_root / "new.md").write_text("third\n", encoding="utf-8")
    called = False

    def forbidden_index(_document):
        nonlocal called
        called = True

    result = ProposalApplyService(database, document_indexer=forbidden_index).accept_and_apply(
        who, proposal.id, revision.id, "pending"
    )
    assert result.apply.apply_status == "conflict"
    assert called is False
    assert (database.knowledge_root / "new.md").read_text(encoding="utf-8") == "third\n"

    for status in (ProposalStatus.REJECTED, ProposalStatus.DEFERRED):
        other_reviews, other_who, other = seed(database)
        other_revision = other_reviews.prepare(other_who, other.id).revision
        other_reviews.decide(other_who, other.id, other_revision.id, status)
    with psycopg.connect(database.database_url) as conn:
        assert conn.execute("SELECT count(*) FROM proposal_applies").fetchone()[0] == 1


def test_apply_ownership_binding_and_terminal_guards(database):
    initialize(database)
    reviews, who, proposal = seed(database)
    revision = reviews.prepare(who, proposal.id).revision
    store = PostgresApplyStore(database)
    store.accept_and_intent(who, proposal.id, revision.id, "pending")
    foreign = ClientIdentity(who.client_type, who.external_chat_id, "foreign")
    with pytest.raises(ReviewForbidden):
        store.get(foreign, proposal.id)
    with pytest.raises(ReviewForbidden):
        store.execute_apply(foreign, proposal.id, SecureKnowledgeWriter(database.knowledge_root))
    store.execute_apply(who, proposal.id, SecureKnowledgeWriter(database.knowledge_root))
    with psycopg.connect(database.database_url, autocommit=True) as conn:
        with pytest.raises(psycopg.Error):
            conn.execute("UPDATE proposal_applies SET review_id=gen_random_uuid()")
        with pytest.raises(psycopg.Error):
            conn.execute("UPDATE proposal_applies SET id=gen_random_uuid()")
        with pytest.raises(psycopg.Error):
            conn.execute("UPDATE proposal_applies SET apply_status='pending', actual_hash=NULL")
        with pytest.raises(psycopg.Error):
            conn.execute("UPDATE proposal_applies SET apply_attempts=apply_attempts+1")
        with pytest.raises(psycopg.Error):
            conn.execute("DELETE FROM proposal_applies")


def test_error_states_cannot_be_reset_to_pending_by_direct_sql(database):
    initialize(database)
    reviews, who, proposal = seed(database)
    revision = reviews.prepare(who, proposal.id).revision
    target = database.knowledge_root / "new.md"
    target.write_text("third\n", encoding="utf-8")
    service = ProposalApplyService(database)
    conflicted = service.accept_and_apply(who, proposal.id, revision.id, "pending")
    assert conflicted.apply.apply_status == "conflict"
    with psycopg.connect(database.database_url, autocommit=True) as conn:
        with pytest.raises(psycopg.Error, match="cannot erase"):
            conn.execute(
                "UPDATE proposal_applies SET apply_status='pending', apply_error_class=NULL"
            )
        conn.execute(
            """UPDATE proposal_applies SET apply_status='applied', actual_hash=expected_new_hash,
               apply_error_class=NULL, applied_at=now(), apply_attempts=apply_attempts+1,
               last_apply_attempt_at=now(), index_status='failed',
               index_error_class='index_operation_failed', index_attempts=1,
               last_index_attempt_at=now()"""
        )
        with pytest.raises(psycopg.Error, match="cannot erase"):
            conn.execute(
                "UPDATE proposal_applies SET index_status='pending', index_error_class=NULL"
            )


def test_conflict_refresh_creates_one_owned_pending_successor(database):
    initialize(database)
    reviews, who, proposal = seed(database)
    revision = reviews.prepare(who, proposal.id).revision
    target = database.knowledge_root / "new.md"
    target.write_text("concurrent base\n", encoding="utf-8")
    service = ProposalApplyService(database, document_indexer=document_indexer(database))
    conflicted = service.accept_and_apply(who, proposal.id, revision.id, "pending")
    assert conflicted.apply.apply_status == "conflict"

    start = Barrier(2)

    def refresh():
        start.wait(timeout=10)
        return service.refresh_review(who, proposal.id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        refreshed = list(pool.map(lambda _: refresh(), range(2)))
    assert refreshed[0].proposal.id == refreshed[1].proposal.id
    successor = refreshed[0]
    assert successor.proposal.status == "pending"
    assert successor.revision.content.old_content == "concurrent base\n"
    assert successor.revision.content.new_content == "new\n"
    original = service.get(who, proposal.id)
    assert original.review.accepted_review_id == revision.id
    assert original.apply.refresh_proposal_id == successor.proposal.id
    with pytest.raises(InvalidTransition):
        # Once a replacement review exists, the immutable conflicted attempt
        # cannot unexpectedly become the winning write.
        service.retry_apply(who, proposal.id)
    applied = service.accept_and_apply(who, successor.proposal.id, successor.revision.id, "pending")
    assert applied.apply.apply_status == "applied"
    assert applied.apply.index_status == "indexed"
    assert target.read_text(encoding="utf-8") == "new\n"
    with psycopg.connect(database.database_url) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM proposal_applies WHERE refresh_proposal_id=%s",
                (successor.proposal.id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM proposal_decisions WHERE proposal_id=%s", (proposal.id,)
            ).fetchone()[0]
            == 1
        )


def test_index_commit_before_status_crash_is_idempotently_confirmed(database):
    initialize(database)
    reviews, who, proposal = seed(database)
    revision = reviews.prepare(who, proposal.id).revision
    store = PostgresApplyStore(database)
    store.accept_and_intent(who, proposal.id, revision.id, "pending")
    store.execute_apply(who, proposal.id, SecureKnowledgeWriter(database.knowledge_root))

    def committed_then_process_loss(document):
        index_document(database, document, embedder=FakeEmbedder())
        raise SimulatedProcessLoss()

    with pytest.raises(SimulatedProcessLoss):
        store.execute_index(
            who,
            proposal.id,
            GitMarkdownSource(database.knowledge_root),
            committed_then_process_loss,
        )
    pending = store.get(who, proposal.id).apply
    assert pending.index_status == "pending" and pending.index_attempts == 1
    retried = store.execute_index(
        who,
        proposal.id,
        GitMarkdownSource(database.knowledge_root),
        document_indexer(database),
    )
    assert retried.apply.index_status == "indexed"
    with psycopg.connect(database.database_url) as conn:
        assert (
            conn.execute("SELECT count(*) FROM chunks WHERE source_path='new.md'").fetchone()[0]
            == 1
        )


def test_document_replace_rolls_back_on_real_postgres_error(database):
    initialize(database)
    source = GitMarkdownSource(database.knowledge_root)
    path = database.knowledge_root / "atomic.md"
    path.write_text("# Old\n\nOld body.", encoding="utf-8")
    old = source.get_document("atomic.md")
    index_document(database, old, embedder=FakeEmbedder())

    class InjectingConnection:
        def __init__(self, conn):
            self.conn = conn

        def transaction(self):
            return self.conn.transaction()

        def execute(self, query, params=()):
            result = self.conn.execute(query, params)
            if query.startswith("DELETE FROM chunks"):
                self.conn.execute("SELECT definitely_missing_v33_index_function()")
            return result

    @contextmanager
    def failing_factory(settings):
        from knowledge_system.db import connect

        with connect(settings) as conn:
            yield InjectingConnection(conn)

    changed = type(old)(old.source_id, old.source_path, "# New\n\nNew body.", old.metadata)
    with pytest.raises(psycopg.Error):
        index_document(
            database,
            changed,
            embedder=FakeEmbedder(),
            connection_factory=failing_factory,
        )
    with psycopg.connect(database.database_url) as conn:
        contents = [
            row[0]
            for row in conn.execute(
                "SELECT content FROM chunks WHERE source_path='atomic.md' ORDER BY ordinal"
            ).fetchall()
        ]
    assert contents and all("Old body" in content for content in contents)
