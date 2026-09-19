"""V3.3 apply/index acceptance against isolated PostgreSQL 17."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import partial
from threading import Barrier, Event, Lock
from types import SimpleNamespace

import psycopg
import pytest

from knowledge_system.apply_schema import APPLY_SCHEMA
from knowledge_system.client_state import ClientIdentity
from knowledge_system.db import init_db
from knowledge_system.index_coordination import (
    GLOBAL_INDEX_LOCK_KEY,
    document_lock_key,
)
from knowledge_system.indexer import (
    DocumentIndexCoordination,
    index_document,
    index_knowledge,
)
from knowledge_system.knowledge_apply import SecureKnowledgeWriter
from knowledge_system.proposal_apply import PostgresApplyStore, ProposalApplyService
from knowledge_system.proposal_review import InvalidTransition, ProposalStatus, ReviewForbidden
from knowledge_system.review_schema import REVIEW_SCHEMA
from knowledge_system.sources import GitMarkdownSource
from tests.integration import test_proposal_review_postgres as pg_review
from tests.integration.test_proposal_review_postgres import seed
from tests.test_indexer import CALLBACK_FORMS, callback_form


@pytest.fixture
def database(tmp_path):
    yield from pg_review.database.__wrapped__(tmp_path)


class FakeEmbedder:
    def encode(self, values):
        return [[float(index + 1)] * 384 for index, _ in enumerate(values)]


def initialize(settings):
    init_db(settings)


def document_indexer(settings):
    return partial(index_document, settings, embedder=FakeEmbedder())


def initial_v33_schema():
    import ast

    original = subprocess.run(
        [
            "git",
            "show",
            "374af059d80258033634404169c51e4945016ba5:src/knowledge_system/apply_schema.py",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return next(
        ast.literal_eval(node.value)
        for node in ast.walk(ast.parse(original))
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "APPLY_SCHEMA" for target in node.targets
        )
    )


def test_apply_schema_is_atomic_on_autocommit_failure(database):
    with psycopg.connect(database.database_url, autocommit=True) as conn:
        for statement in pg_review.v312_statements():
            conn.execute(statement)
        conn.execute(REVIEW_SCHEMA)
        with pytest.raises(psycopg.Error):
            conn.execute(APPLY_SCHEMA + "\nSELECT definitely_missing_v33_function();")
        assert conn.execute("SELECT to_regclass('proposal_applies')").fetchone()[0] is None


def test_existing_v33_schema_migration_is_repeatable_and_atomic(database):
    with psycopg.connect(database.database_url, autocommit=True) as conn:
        for statement in pg_review.v312_statements():
            conn.execute(statement)
        conn.execute(REVIEW_SCHEMA)
        conn.execute(initial_v33_schema())

    reviews, who, proposal = seed(database)
    revision = reviews.prepare(who, proposal.id).revision
    reviews.decide(who, proposal.id, revision.id, ProposalStatus.ACCEPTED)
    PostgresApplyStore(database).ensure_intent(who, proposal.id, revision.id)
    with psycopg.connect(database.database_url, autocommit=True) as conn:
        conn.execute(
            """UPDATE proposal_applies SET apply_status='applied',
            actual_hash=expected_new_hash, apply_attempts=1,
            last_apply_attempt_at=now(), applied_at=now()"""
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM pg_constraint WHERE conname=%s",
                ("proposal_applies_apply_result_check",),
            ).fetchone()[0]
            == 0
        )
        with pytest.raises(psycopg.Error, match="definitely_missing_v33_migration"):
            conn.execute(APPLY_SCHEMA + "\nSELECT definitely_missing_v33_migration();")
        assert (
            conn.execute(
                "SELECT count(*) FROM pg_constraint WHERE conname=%s",
                ("proposal_applies_apply_result_check",),
            ).fetchone()[0]
            == 0
        )
        conn.execute(APPLY_SCHEMA)
        conn.execute(APPLY_SCHEMA)
        definition = conn.execute(
            """SELECT pg_get_constraintdef(oid) FROM pg_constraint
            WHERE conname='proposal_applies_apply_result_check'"""
        ).fetchone()[0]
        assert "actual_hash IS NOT NULL" in definition
        assert conn.execute(
            "SELECT actual_hash=expected_new_hash FROM proposal_applies"
        ).fetchone()[0]


def test_existing_corrupt_v33_success_blocks_migration_without_repair(database):
    with psycopg.connect(database.database_url, autocommit=True) as conn:
        for statement in pg_review.v312_statements():
            conn.execute(statement)
        conn.execute(REVIEW_SCHEMA)
        conn.execute(initial_v33_schema())
    reviews, who, proposal = seed(database)
    revision = reviews.prepare(who, proposal.id).revision
    PostgresApplyStore(database).accept_and_intent(who, proposal.id, revision.id, "pending")
    with psycopg.connect(database.database_url, autocommit=True) as conn:
        conn.execute(
            """UPDATE proposal_applies SET apply_status='applied', actual_hash=NULL,
            applied_at=now(), apply_attempts=1, last_apply_attempt_at=now()"""
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(APPLY_SCHEMA)
        row = conn.execute("SELECT apply_status, actual_hash FROM proposal_applies").fetchone()
        assert row == ("applied", None)
        assert (
            conn.execute(
                "SELECT count(*) FROM pg_constraint WHERE conname=%s",
                ("proposal_applies_apply_result_check",),
            ).fetchone()[0]
            == 0
        )


def test_applied_rows_require_a_matching_nonnull_hash_on_insert_and_update(database):
    initialize(database)
    reviews, who, proposal = seed(database)
    revision = reviews.prepare(who, proposal.id).revision
    reviews.decide(who, proposal.id, revision.id, ProposalStatus.ACCEPTED)
    with (
        psycopg.connect(database.database_url, autocommit=True) as conn,
        pytest.raises(psycopg.errors.CheckViolation),
    ):
        conn.execute(
            """INSERT INTO proposal_applies
                (id, proposal_id, review_id, target_source_id, target_source_path,
                 expected_old_hash, expected_absent, expected_new_hash, apply_status,
                 actual_hash, apply_attempts, last_apply_attempt_at, applied_at,
                 actor_client_type, actor_external_chat_id, actor_external_user_id)
                VALUES (gen_random_uuid(),%s,%s,%s,%s,%s,%s,%s,'applied',NULL,1,
                        now(),now(),%s,%s,%s)""",
            (
                proposal.id,
                revision.id,
                revision.content.target_source_id,
                revision.content.target_source_path,
                revision.content.old_hash,
                revision.content.change_kind == "create",
                revision.content.new_hash,
                who.client_type,
                who.external_chat_id,
                who.external_user_id,
            ),
        )

    store = PostgresApplyStore(database)
    store.ensure_intent(who, proposal.id, revision.id)
    with psycopg.connect(database.database_url, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """UPDATE proposal_applies SET apply_status='applied', actual_hash=NULL,
                apply_attempts=1, last_apply_attempt_at=now(), applied_at=now()"""
            )
        conn.execute(
            """UPDATE proposal_applies SET apply_status='applied',
            actual_hash=expected_new_hash, apply_attempts=1,
            last_apply_attempt_at=now(), applied_at=now()"""
        )
        assert conn.execute(
            "SELECT actual_hash=expected_new_hash FROM proposal_applies"
        ).fetchone()[0]


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

    def index(document, *, coordination):
        nonlocal calls
        with lock:
            calls += 1
        return index_document(
            database,
            document,
            coordination=coordination,
            embedder=FakeEmbedder(),
        )

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


def test_public_document_indexer_di_receives_held_lock_state(database):
    initialize(database)
    reviews, who, proposal = seed(database)
    revision = reviews.prepare(who, proposal.id).revision
    script = r"""
import sys
from functools import partial
from pathlib import Path
from uuid import UUID
from knowledge_system.client_state import ClientIdentity
from knowledge_system.config import Settings
from knowledge_system.indexer import index_document
from knowledge_system.proposal_apply import ProposalApplyService

class Embedder:
    def encode(self, values):
        return [[float(index + 1)] * 384 for index, _ in enumerate(values)]

settings = Settings(sys.argv[1], Path(sys.argv[2]), "unused", 384)
service = ProposalApplyService(
    settings,
    document_indexer=partial(index_document, settings, embedder=Embedder()),
)
result = service.accept_and_apply(
    ClientIdentity("test", "chat", "user"), UUID(sys.argv[3]), UUID(sys.argv[4]), "pending"
)
print(result.apply.apply_status, result.apply.index_status)
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            database.database_url,
            str(database.knowledge_root),
            str(proposal.id),
            str(revision.id),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.stdout.rstrip().endswith("applied indexed")
    with psycopg.connect(database.database_url) as conn:
        assert (
            conn.execute("SELECT count(*) FROM chunks WHERE source_path='new.md'").fetchone()[0]
            == 1
        )


def test_incompatible_document_indexer_is_rejected_before_attempt(database):
    initialize(database)
    with pytest.raises(TypeError, match="keyword-only coordination"):
        ProposalApplyService(database, document_indexer=lambda document: None)

    reviews, who, proposal = seed(database)
    revision = reviews.prepare(who, proposal.id).revision
    store = PostgresApplyStore(database)
    store.accept_and_intent(who, proposal.id, revision.id, "pending")
    store.execute_apply(who, proposal.id, SecureKnowledgeWriter(database.knowledge_root))

    with pytest.raises(TypeError, match="keyword-only coordination"):
        store.execute_index(
            who,
            proposal.id,
            GitMarkdownSource(database.knowledge_root),
            lambda document: None,
        )
    current = store.get(who, proposal.id).apply
    assert current.index_status == "pending"
    assert current.index_attempts == 0


@pytest.mark.parametrize("kind", CALLBACK_FORMS)
@pytest.mark.parametrize("operation", ["accept", "apply_accepted", "retry_apply", "retry_index"])
def test_changed_incompatible_callback_is_rejected_before_any_mutation(database, kind, operation):
    initialize(database)
    reviews, who, proposal = seed(database)
    revision = reviews.prepare(who, proposal.id).revision
    store = PostgresApplyStore(database)
    service = ProposalApplyService(
        database, store=store, document_indexer=document_indexer(database)
    )
    if operation != "accept":
        store.accept_and_intent(who, proposal.id, revision.id, "pending")
    if operation == "retry_index":
        store.execute_apply(who, proposal.id, SecureKnowledgeWriter(database.knowledge_root))
    before = store.get(who, proposal.id)
    target = database.knowledge_root / "new.md"
    before_bytes = target.read_bytes() if target.exists() else None
    service.document_indexer = callback_form(kind, False)
    with pytest.raises(TypeError, match="keyword-only coordination"):
        if operation == "accept":
            service.accept_and_apply(who, proposal.id, revision.id, "pending")
        elif operation == "apply_accepted":
            service.apply_accepted(who, proposal.id, revision.id)
        else:
            getattr(service, operation)(who, proposal.id)
    assert store.get(who, proposal.id) == before
    assert (target.read_bytes() if target.exists() else None) == before_bytes
    with psycopg.connect(database.database_url) as conn:
        assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0


@pytest.mark.parametrize("kind", CALLBACK_FORMS)
def test_compatible_callback_forms_index_exactly_once(database, kind):
    initialize(database)
    reviews, who, proposal = seed(database)
    revision = reviews.prepare(who, proposal.id).revision
    service = ProposalApplyService(
        database, document_indexer=callback_form(kind, True, document_indexer(database))
    )
    with psycopg.connect(database.database_url) as conn:
        assert conn.execute("SHOW statement_timeout").fetchone()[0] == "0"
    result = service.accept_and_apply(who, proposal.id, revision.id, "pending")
    assert result.apply.index_status == "indexed"
    assert service.retry_index(who, proposal.id).apply == result.apply
    with psycopg.connect(database.database_url) as conn:
        assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 1


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


def test_apply_journal_stays_failed_until_retry_resynchronizes(database, monkeypatch):
    initialize(database)
    reviews, who, proposal = seed(database)
    revision = reviews.prepare(who, proposal.id).revision
    store = PostgresApplyStore(database)
    store.accept_and_intent(who, proposal.id, revision.id, "pending")
    real_fsync = os.fsync
    directory_calls = 0

    def fail_directory(fd):
        nonlocal directory_calls
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_calls += 1
            raise OSError()
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory)
    for expected_attempts in (1, 2):
        result = store.execute_apply(
            who, proposal.id, SecureKnowledgeWriter(database.knowledge_root)
        ).apply
        assert result.apply_status == "failed"
        assert result.actual_hash is None
        assert result.apply_attempts == expected_attempts
    assert directory_calls == 2

    monkeypatch.setattr(os, "fsync", real_fsync)
    healed = store.execute_apply(
        who, proposal.id, SecureKnowledgeWriter(database.knowledge_root)
    ).apply
    assert healed.apply_status == "applied"
    assert healed.actual_hash == revision.content.new_hash
    assert healed.apply_attempts == 3


@pytest.mark.parametrize("old", [None, "old\n"])
@pytest.mark.parametrize("replacement_bytes", [b"new\n", b"different\n"])
@pytest.mark.parametrize("retry", [False, True])
def test_retry_inode_exchange_never_records_applied(
    database, monkeypatch, old, replacement_bytes, retry
):
    initialize(database)
    target = database.knowledge_root / "new.md"
    if old is not None:
        target.write_text(old, encoding="utf-8")
        target.chmod(0o640)
    reviews, who, proposal = seed(database)
    revision = reviews.prepare(who, proposal.id).revision
    index_calls = []

    def index(document, *, coordination):
        index_calls.append(document)

    service = ProposalApplyService(
        database,
        document_indexer=index,
        writer=SecureKnowledgeWriter(database.knowledge_root, require_private_permissions=True),
    )
    real_fsync = os.fsync

    def fail_first_parent(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError()
        return real_fsync(fd)

    if retry:
        monkeypatch.setattr(os, "fsync", fail_first_parent)
        first = service.accept_and_apply(who, proposal.id, revision.id, "pending")
        assert first.apply.apply_status == "failed"
        assert target.read_bytes() == b"new\n"

    synced_inodes = set()
    replacement_inode = None

    def exchange_on_parent_fsync(fd):
        nonlocal replacement_inode
        info = os.fstat(fd)
        if stat.S_ISREG(info.st_mode):
            synced_inodes.add((info.st_dev, info.st_ino))
        elif replacement_inode is None:
            replacement = database.knowledge_root / ".replacement"
            replacement.write_bytes(replacement_bytes)
            replacement.chmod(stat.S_IMODE(target.stat().st_mode))
            os.replace(replacement, target)
            final = target.stat()
            replacement_inode = (final.st_dev, final.st_ino)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", exchange_on_parent_fsync)
    result = (
        service.retry_apply(who, proposal.id)
        if retry
        else service.accept_and_apply(who, proposal.id, revision.id, "pending")
    )
    assert result.apply.apply_status == "conflict"
    assert result.apply.actual_hash is None
    assert result.apply.index_status == "pending"
    assert result.apply.index_attempts == 0
    assert index_calls == []
    assert replacement_inode is not None
    assert replacement_inode not in synced_inodes
    with psycopg.connect(database.database_url) as conn:
        assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0


def test_index_failure_keeps_applied_file_and_retry_replaces_chunks(database):
    initialize(database)
    reviews, who, proposal = seed(database)
    revision = reviews.prepare(who, proposal.id).revision
    store = PostgresApplyStore(database)
    store.accept_and_intent(who, proposal.id, revision.id, "pending")
    applied = store.execute_apply(who, proposal.id, SecureKnowledgeWriter(database.knowledge_root))
    assert applied.apply.apply_status == "applied"

    def committed_then_lost_confirmation(document, *, coordination):
        index_document(
            database,
            document,
            coordination=coordination,
            embedder=FakeEmbedder(),
        )
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

    def forbidden_index(_document, *, coordination):
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


def test_conflict_refresh_preserves_accepted_edits_across_repeated_refresh(database):
    initialize(database)
    reviews, who, proposal = seed(database)
    revision = reviews.prepare(
        who,
        proposal.id,
        target_source_path="edited.md",
        new_content="human correction\n",
    ).revision
    target = database.knowledge_root / "edited.md"
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
    assert successor.revision.content.target_source_path == "edited.md"
    assert successor.revision.content.new_content == "human correction\n"
    original = service.get(who, proposal.id)
    assert original.review.accepted_review_id == revision.id
    assert original.apply.refresh_proposal_id == successor.proposal.id
    with pytest.raises(InvalidTransition):
        # Once a replacement review exists, the immutable conflicted attempt
        # cannot unexpectedly become the winning write.
        service.retry_apply(who, proposal.id)
    target.write_text("second concurrent base\n", encoding="utf-8")
    repeated_conflict = service.accept_and_apply(
        who, successor.proposal.id, successor.revision.id, "pending"
    )
    assert repeated_conflict.apply.apply_status == "conflict"
    repeated = service.refresh_review(who, successor.proposal.id)
    assert repeated.revision.content.target_source_path == "edited.md"
    assert repeated.revision.content.new_content == "human correction\n"
    applied = service.accept_and_apply(who, repeated.proposal.id, repeated.revision.id, "pending")
    assert applied.apply.apply_status == "applied"
    assert applied.apply.index_status == "indexed"
    assert target.read_text(encoding="utf-8") == "human correction\n"
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

    def committed_then_process_loss(document, *, coordination):
        index_document(
            database,
            document,
            coordination=coordination,
            embedder=FakeEmbedder(),
        )
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
    index_document(
        database,
        old,
        coordination=DocumentIndexCoordination.ACQUIRE_LOCKS,
        embedder=FakeEmbedder(),
    )

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
    path.write_text(changed.content, encoding="utf-8")
    with pytest.raises(psycopg.Error):
        index_document(
            database,
            changed,
            coordination=DocumentIndexCoordination.ACQUIRE_LOCKS,
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


@pytest.mark.parametrize("first_path,second_path", [("note.md", "Note.md"), ("Note.md", "note.md")])
def test_parallel_case_alias_creates_exactly_one_file(
    database, monkeypatch, first_path, second_path
):
    initialize(database)
    jobs = []
    store = PostgresApplyStore(database)
    for path in (first_path, second_path):
        reviews, who, proposal = seed(database)
        revision = reviews.prepare(who, proposal.id, target_source_path=path).revision
        store.accept_and_intent(who, proposal.id, revision.id, "pending")
        jobs.append((who, proposal.id))

    from knowledge_system import proposal_apply

    real_lock = proposal_apply.lock_document_index
    first_acquired = Event()
    second_attempted = Event()
    release_first = Event()
    calls = 0
    call_lock = Lock()

    def controlled_lock(conn, source_id, source_path):
        nonlocal calls
        with call_lock:
            calls += 1
            number = calls
        if number == 2:
            second_attempted.set()
        real_lock(conn, source_id, source_path)
        if number == 1:
            first_acquired.set()
            assert release_first.wait(timeout=10)

    monkeypatch.setattr(proposal_apply, "lock_document_index", controlled_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            store.execute_apply,
            jobs[0][0],
            jobs[0][1],
            SecureKnowledgeWriter(database.knowledge_root),
        )
        assert first_acquired.wait(timeout=10)
        second = pool.submit(
            store.execute_apply,
            jobs[1][0],
            jobs[1][1],
            SecureKnowledgeWriter(database.knowledge_root),
        )
        assert second_attempted.wait(timeout=10)
        release_first.set()
        statuses = [
            first.result(timeout=10).apply.apply_status,
            second.result(timeout=10).apply.apply_status,
        ]

    assert statuses == ["applied", "conflict"]
    assert [path.name for path in database.knowledge_root.iterdir()] == [first_path]


def test_former_hashtext_collision_paths_take_independent_document_locks(database):
    initialize(database)
    first = document_lock_key("knowledge-git", "p3059.md")
    second = document_lock_key("knowledge-git", "p98538.md")
    assert first != second

    with (
        psycopg.connect(database.database_url) as first_conn,
        psycopg.connect(database.database_url) as second_conn,
    ):
        first_conn.execute("SELECT pg_advisory_xact_lock_shared(%s)", (GLOBAL_INDEX_LOCK_KEY,))
        first_conn.execute("SELECT pg_advisory_xact_lock(%s)", (first,))
        assert second_conn.execute(
            "SELECT pg_try_advisory_xact_lock_shared(%s)", (GLOBAL_INDEX_LOCK_KEY,)
        ).fetchone()[0]
        assert second_conn.execute("SELECT pg_try_advisory_xact_lock(%s)", (second,)).fetchone()[0]


def test_standalone_discards_snapshot_captured_before_successful_apply(database):
    initialize(database)
    target = database.knowledge_root / "new.md"
    target.write_text("old\n", encoding="utf-8")
    stale = GitMarkdownSource(database.knowledge_root).get_document("new.md")
    reviews, who, proposal = seed(database)
    revision = reviews.prepare(who, proposal.id).revision
    service = ProposalApplyService(database, document_indexer=document_indexer(database))
    assert (
        service.accept_and_apply(who, proposal.id, revision.id, "pending").apply.index_status
        == "indexed"
    )
    entered, release = Event(), Event()
    embedded = []

    class PausedEmbedder:
        def encode(self, values):
            embedded.extend(values)
            entered.set()
            assert release.wait(10)
            return FakeEmbedder().encode(values)

    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(
            index_document,
            database,
            stale,
            coordination=DocumentIndexCoordination.ACQUIRE_LOCKS,
            embedder=PausedEmbedder(),
        )
        try:
            assert entered.wait(10)
            assert embedded == ["(document)\n\nnew"]
        finally:
            release.set()
        assert task.result(timeout=10) == 1
    assert target.read_text(encoding="utf-8") == "new\n"
    assert service.retry_index(who, proposal.id).apply.index_status == "indexed"
    with psycopg.connect(database.database_url) as conn:
        assert conn.execute("SELECT content FROM chunks").fetchall() == [("(document)\n\nnew",)]


def test_standalone_holds_locks_before_snapshot_through_commit(database, monkeypatch):
    initialize(database)
    target = database.knowledge_root / "new.md"
    target.write_text("old\n", encoding="utf-8")
    source = GitMarkdownSource(database.knowledge_root)
    stale = source.get_document("new.md")
    reviews, who, proposal = seed(database)
    revision = reviews.prepare(who, proposal.id).revision
    service = ProposalApplyService(database, document_indexer=document_indexer(database))
    service.store.accept_and_intent(who, proposal.id, revision.id, "pending")
    entered, release, apply_attempted = Event(), Event(), Event()
    key = document_lock_key("knowledge-git", "new.md")

    def assert_locks_held():
        with psycopg.connect(database.database_url) as conn:
            assert conn.execute("SHOW statement_timeout").fetchone()[0] == "0"
            assert not conn.execute("SELECT pg_try_advisory_xact_lock(%s)", (key,)).fetchone()[0]
            assert not conn.execute(
                "SELECT pg_try_advisory_xact_lock(%s)", (GLOBAL_INDEX_LOCK_KEY,)
            ).fetchone()[0]

    real_snapshot = GitMarkdownSource.snapshot

    def snapshot(self, source_id, path):
        assert_locks_held()
        return real_snapshot(self, source_id, path)

    monkeypatch.setattr(GitMarkdownSource, "snapshot", snapshot)

    class PausedEmbedder:
        def encode(self, values):
            assert values == ["(document)\n\nold"]
            assert_locks_held()
            entered.set()
            assert release.wait(10)
            return FakeEmbedder().encode(values)

    from knowledge_system import proposal_apply

    real_lock = proposal_apply.lock_document_index

    def observed_lock(conn, source_id, path):
        apply_attempted.set()
        real_lock(conn, source_id, path)

    monkeypatch.setattr(proposal_apply, "lock_document_index", observed_lock)

    # Prove the standalone transaction still owns its locks at the commit edge.
    from knowledge_system.db import connect

    class ObservedConnection:
        def __init__(self, conn):
            self.conn = conn

        @contextmanager
        def transaction(self):
            with self.conn.transaction():
                yield
                assert_locks_held()

        def execute(self, *args, **kwargs):
            return self.conn.execute(*args, **kwargs)

    @contextmanager
    def connection_factory(settings):
        with connect(settings) as conn:
            yield ObservedConnection(conn)

    with ThreadPoolExecutor(max_workers=2) as pool:
        standalone = pool.submit(
            index_document,
            database,
            stale,
            coordination=DocumentIndexCoordination.ACQUIRE_LOCKS,
            embedder=PausedEmbedder(),
            connection_factory=connection_factory,
        )
        try:
            assert entered.wait(10)
            apply = pool.submit(service.retry_apply, who, proposal.id)
            assert apply_attempted.wait(10)
            assert target.read_text(encoding="utf-8") == "old\n"
            with psycopg.connect(database.database_url) as conn:
                assert (
                    conn.execute("SELECT index_status FROM proposal_applies").fetchone()[0]
                    == "pending"
                )
        finally:
            release.set()
        assert standalone.result(timeout=10) == 1
        assert apply.result(timeout=10).apply.index_status == "indexed"
    assert target.read_text(encoding="utf-8") == "new\n"
    with psycopg.connect(database.database_url) as conn:
        assert conn.execute("SELECT content FROM chunks").fetchall() == [("(document)\n\nnew",)]


def test_full_index_and_parallel_applies_use_one_consistent_lock_order(database, monkeypatch):
    initialize(database)
    contents = {
        "a.md": ("# A\n\nold A\n", "# A\n\naccepted A\n"),
        "b.md": ("# B\n\nold B\n", "# B\n\naccepted B\n"),
    }
    source = GitMarkdownSource(database.knowledge_root)
    for path, (old, _) in contents.items():
        (database.knowledge_root / path).write_text(old, encoding="utf-8")
        index_document(
            database,
            source.get_document(path),
            coordination=DocumentIndexCoordination.ACQUIRE_LOCKS,
            embedder=FakeEmbedder(),
        )

    jobs = []
    store = PostgresApplyStore(database)
    for path, (_, new) in contents.items():
        reviews, who, proposal = seed(database)
        revision = reviews.prepare(
            who, proposal.id, target_source_path=path, new_content=new
        ).revision
        store.accept_and_intent(who, proposal.id, revision.id, "pending")
        jobs.append((who, proposal.id))

    first_snapshotted = Event()
    release_snapshot = Event()

    class PausedSource:
        source_id = "knowledge-git"

        def discover(self):
            first = source.get_document("a.md")
            first_snapshotted.set()
            assert release_snapshot.wait(timeout=10)
            return [first, source.get_document("b.md")]

    monkeypatch.setitem(
        sys.modules,
        "knowledge_system.embedder",
        SimpleNamespace(LocalEmbedder=lambda *_args: FakeEmbedder()),
    )
    from knowledge_system import proposal_apply

    real_lock = proposal_apply.lock_document_index
    apply_lock_attempts = Barrier(3)
    observed_calls = 0
    observed_calls_lock = Lock()

    def observed_lock(conn, source_id, source_path):
        nonlocal observed_calls
        with observed_calls_lock:
            observed_calls += 1
            number = observed_calls
        if number <= 2:
            apply_lock_attempts.wait(timeout=10)
        real_lock(conn, source_id, source_path)

    monkeypatch.setattr(proposal_apply, "lock_document_index", observed_lock)
    service = ProposalApplyService(
        database, store=store, document_indexer=document_indexer(database)
    )
    with ThreadPoolExecutor(max_workers=3) as pool:
        full = pool.submit(index_knowledge, database, PausedSource())
        assert first_snapshotted.wait(timeout=10)
        applies = [pool.submit(service.retry_apply, who, pid) for who, pid in jobs]
        apply_lock_attempts.wait(timeout=10)
        with psycopg.connect(database.database_url) as conn:
            assert conn.execute(
                "SELECT array_agg(index_status ORDER BY proposal_id) FROM proposal_applies"
            ).fetchone()[0] == ["pending", "pending"]
        release_snapshot.set()
        full.result(timeout=10)
        results = [future.result(timeout=10) for future in applies]

    assert all(result.apply.index_status == "indexed" for result in results)
    with psycopg.connect(database.database_url) as conn:
        indexed = dict(
            conn.execute(
                "SELECT source_path, content FROM chunks WHERE source_path=ANY(%s)",
                (list(contents),),
            ).fetchall()
        )
    assert indexed == {
        "a.md": "A\n\naccepted A",
        "b.md": "B\n\naccepted B",
    }
    for who, pid in jobs:
        assert service.retry_apply(who, pid).apply.index_status == "indexed"
