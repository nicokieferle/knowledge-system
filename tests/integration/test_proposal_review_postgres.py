"""Opt-in real PostgreSQL; only a dedicated loopback test database is accepted."""

import ast
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql

from knowledge_system.client_state import ClientIdentity, PostgresClientStateStore
from knowledge_system.config import Settings
from knowledge_system.conversation_models import MessageRole, ProposalDraft, ProposalTriggerType
from knowledge_system.conversation_schema import init_conversation_schema
from knowledge_system.conversation_store import PostgresConversationStore
from knowledge_system.proposal_apply import PostgresApplyStore
from knowledge_system.proposal_review import (
    InvalidTransition,
    ProposalNotFound,
    ProposalReviewService,
    ProposalStatus,
    ReviewConflict,
    ReviewForbidden,
    ReviewMissing,
)
from knowledge_system.proposal_store import PostgresProposalStore, ProposalCreate
from knowledge_system.review_schema import REVIEW_SCHEMA
from knowledge_system.review_store import PostgresReviewStore
from knowledge_system.sources import GitMarkdownSource


@pytest.fixture
def database(tmp_path):
    address = os.getenv("TEST_V32_DATABASE_URL")
    if not address:
        pytest.skip("TEST_V32_DATABASE_URL requires isolated local PostgreSQL")
    parsed = urlsplit(address)
    assert parsed.hostname in ("localhost", "127.0.0.1")
    assert parsed.path == "/knowledge_v32_test" and not parsed.query
    schema = "v32_" + uuid4().hex
    with psycopg.connect(address, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    from urllib.parse import urlencode

    scoped = address + "?" + urlencode({"options": f"-csearch_path={schema},public"})
    settings = Settings(scoped, tmp_path, "unused", 384)
    try:
        yield settings
    finally:
        with psycopg.connect(address, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def initialize(settings):
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        init_conversation_schema(conn)


def v312_statements():
    original = subprocess.run(
        [
            "git",
            "show",
            "92b25474b94a3d4a369e50363cc3df68a2fba62c:src/knowledge_system/conversation_schema.py",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return next(
        ast.literal_eval(node.value)
        for node in ast.walk(ast.parse(original))
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "statements" for t in node.targets)
    )


def test_review_schema_is_atomic_on_autocommit_failure(database):
    with psycopg.connect(database.database_url, autocommit=True) as conn:
        for statement in v312_statements():
            conn.execute(statement)
        with pytest.raises(psycopg.Error):
            conn.execute(REVIEW_SCHEMA + "\nSELECT definitely_missing_v32_function();")
        assert conn.execute("SELECT to_regclass('proposal_reviews')").fetchone()[0] is None
        status = conn.execute(
            """SELECT pg_get_constraintdef(oid) FROM pg_constraint
            WHERE conrelid='proposals'::regclass AND conname='proposals_status_check'"""
        ).fetchone()[0]
        assert "accepted" not in status


def seed(settings):
    convs = PostgresConversationStore(settings)
    c = convs.create_conversation("test", "Synthetic")
    who = ClientIdentity("test", "chat", "user")
    states = PostgresClientStateStore(settings)
    states.activate(who, c.id)
    m = convs.add_message(c.id, MessageRole.USER, "Synthetic control")
    p = PostgresProposalStore(settings).create_proposal(
        ProposalCreate(
            c.id,
            "test",
            ProposalTriggerType.COMMAND,
            m.id,
            (),
            ProposalDraft(
                "Summary",
                "Reason",
                "new\n",
                target_source_id="knowledge-git",
                target_source_path="new.md",
                base_revision="invented",
            ),
        )
    )
    service = ProposalReviewService(
        PostgresReviewStore(settings), GitMarkdownSource(settings.knowledge_root)
    )
    return service, who, p


def test_migrates_real_v312_schema_preserves_proposal_and_is_repeatable(database):
    with psycopg.connect(database.database_url, autocommit=True) as conn:
        for statement in v312_statements():
            conn.execute(statement)
    service, who, p = seed(database)
    initialize(database)
    initialize(database)
    assert service.get(who, p.id).proposal == p
    view = service.prepare(who, p.id)
    result = service.decide(who, p.id, view.revision.id, ProposalStatus.ACCEPTED)
    assert result.accepted_review_id == view.revision.id
    with psycopg.connect(database.database_url) as conn:
        deferred = conn.execute(
            """SELECT condeferrable, condeferred, convalidated FROM pg_constraint
            WHERE conrelid='proposals'::regclass
              AND conname='proposals_accepted_review_fk'"""
        ).fetchone()
    assert deferred == (True, True, True)


@pytest.mark.parametrize(
    "first,last",
    [
        ("pending", "accepted"),
        ("pending", "rejected"),
        ("pending", "deferred"),
        ("deferred", "accepted"),
        ("deferred", "rejected"),
    ],
)
def test_lifecycle_and_terminal_retry(database, first, last):
    initialize(database)
    service, who, p = seed(database)
    r = service.prepare(who, p.id).revision
    if first == "deferred":
        service.decide(who, p.id, r.id, ProposalStatus.DEFERRED)
    result = service.decide(who, p.id, r.id, ProposalStatus(last))
    assert result.proposal.status == last
    assert service.decide(who, p.id, r.id, ProposalStatus(last)) == result
    if last in ("accepted", "rejected"):
        with pytest.raises(InvalidTransition):
            service.decide(
                who, p.id, r.id, ProposalStatus("rejected" if last == "accepted" else "accepted")
            )
        with pytest.raises(InvalidTransition):
            service.prepare(who, p.id, new_content="changed")
    with psycopg.connect(database.database_url) as conn:
        count = conn.execute(
            "SELECT count(*) FROM proposal_decisions WHERE proposal_id=%s", (p.id,)
        ).fetchone()[0]
    assert count == (2 if first == "deferred" else 1)


def test_ownership_unknown_missing_review_and_revision_binding(database):
    initialize(database)
    service, who, p = seed(database)
    other = ClientIdentity("test", "chat", "other")
    with pytest.raises(ReviewForbidden):
        service.get(other, p.id)
    with pytest.raises(ReviewForbidden):
        service.list(other, p.conversation_id)
    with pytest.raises(ProposalNotFound):
        service.get(who, uuid4())
    with pytest.raises(ReviewMissing):
        service.decide(who, p.id, None, ProposalStatus.ACCEPTED)
    r1 = service.prepare(who, p.id).revision
    r2 = service.prepare(who, p.id, new_content="another\n").revision
    assert r1.id != r2.id and r2.number == 2
    with pytest.raises(ReviewForbidden):
        service.decide_revision(other, r2.id, ProposalStatus.ACCEPTED)
    with pytest.raises(ReviewConflict):
        service.decide_revision(who, r1.id, ProposalStatus.ACCEPTED)
    service.decide_revision(who, r2.id, ProposalStatus.ACCEPTED)
    with psycopg.connect(database.database_url, autocommit=True) as conn:
        assert (
            conn.execute(
                "SELECT new_content FROM proposal_reviews WHERE id=%s", (r1.id,)
            ).fetchone()[0]
            == "new\n"
        )
        with pytest.raises(psycopg.Error):
            conn.execute("UPDATE proposal_reviews SET new_content='tampered' WHERE id=%s", (r2.id,))
        with pytest.raises(psycopg.Error):
            conn.execute(
                "UPDATE proposal_decisions SET status='rejected' WHERE proposal_id=%s", (p.id,)
            )


def test_concurrent_decisions_exactly_one_wins(database):
    initialize(database)
    service, who, p = seed(database)
    r = service.prepare(who, p.id).revision

    def decide(status):
        try:
            return service.decide_revision(who, r.id, status).proposal.status
        except InvalidTransition:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(decide, [ProposalStatus.ACCEPTED, ProposalStatus.REJECTED]))
    assert results.count("conflict") == 1
    with psycopg.connect(database.database_url) as conn:
        assert conn.execute("SELECT count(*) FROM proposal_decisions").fetchone()[0] == 1


def test_concurrent_prepare_idempotent_and_restart(database):
    initialize(database)
    service, who, p = seed(database)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.prepare(who, p.id), range(2)))
    assert results[0].revision == results[1].revision
    restarted = ProposalReviewService(
        PostgresReviewStore(database), GitMarkdownSource(database.knowledge_root)
    )
    assert restarted.get(who, p.id) == results[0]


@pytest.mark.parametrize("other", [ProposalStatus.ACCEPTED, ProposalStatus.REJECTED])
def test_concurrent_defer_and_terminal_decision_use_status_cas(database, other):
    initialize(database)
    service, who, p = seed(database)
    r = service.prepare(who, p.id).revision

    def decide(status):
        try:
            return service.store.decide(who, p.id, r.id, status, "pending").proposal.status
        except (ReviewConflict, InvalidTransition):
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(decide, [ProposalStatus.DEFERRED, other]))
    assert results.count("conflict") == 1


def test_legacy_telegram_callback_retry_never_mutates_durable_state(database):
    from knowledge_system.telegram_review import TelegramReview
    from tests.test_telegram_adapter import FakeTransport

    initialize(database)
    service, who, p = seed(database)
    r = service.prepare(who, p.id).revision
    transport = FakeTransport()
    ui = TelegramReview(transport)
    data = f"rv:a:{r.id.hex}"
    ui.callback(who, "callback-1", data)
    ui.callback(who, "callback-1", data)
    assert transport.callbacks[0] == transport.callbacks[1]
    with psycopg.connect(database.database_url, autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM proposal_decisions").fetchone()[0] == 0
        assert service.get(who, p.id).proposal.status == "pending"
        service.decide(who, p.id, r.id, ProposalStatus.ACCEPTED)
        with pytest.raises(psycopg.Error):
            conn.execute(
                "UPDATE proposals SET status='pending', accepted_review_id=NULL WHERE id=%s",
                (p.id,),
            )


def test_review_fingerprint_covers_revision_and_decision(database):
    from knowledge_system.durable_state import read_durable_state_fingerprint

    initialize(database)
    service, who, p = seed(database)
    before = read_durable_state_fingerprint(database)
    r = service.prepare(who, p.id).revision
    prepared = read_durable_state_fingerprint(database)
    service.decide(who, p.id, r.id, ProposalStatus.ACCEPTED)
    accepted = read_durable_state_fingerprint(database)
    assert len({before.sha256, prepared.sha256, accepted.sha256}) == 3
    assert accepted.counts["proposal_reviews"] == 1
    assert accepted.counts["proposal_decisions"] == 1


def test_real_archive_restore_preserves_accepted_revision(database):
    from dataclasses import replace
    from urllib.parse import urlunsplit

    from knowledge_system.durable_state import DURABLE_TABLES, read_durable_state_fingerprint

    container = os.getenv("TEST_V32_CONTAINER")
    if not container:
        pytest.skip("TEST_V32_CONTAINER required for local archive tools")
    assert container.startswith("knowledge-v32-review-")
    label = subprocess.run(
        ["docker", "inspect", "--format", '{{index .Config.Labels "codex.task"}}', container],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert label == "v32-review"
    from scripts.conversation_postgres_smoke import run_smoke

    run_smoke(database)
    service, who, p = seed(database)
    first = service.prepare(who, p.id).revision
    accepted = service.prepare(who, p.id, new_content="accepted second revision\n").revision
    assert first.number == 1 and accepted.number == 2
    service.decide(who, p.id, accepted.id, ProposalStatus.ACCEPTED)
    # Every durable table must contain representative data in the archive
    # roundtrip; an accepted legacy proposal is still never applied implicitly.
    PostgresApplyStore(database).ensure_intent(who, p.id, accepted.id)
    deferred_service, deferred_who, deferred_proposal = seed(database)
    deferred_revision = deferred_service.prepare(deferred_who, deferred_proposal.id).revision
    deferred_service.decide(
        deferred_who, deferred_proposal.id, deferred_revision.id, ProposalStatus.DEFERRED
    )
    from knowledge_system.proposal_ingress import ProposalIngressService
    from tests.integration.test_proposal_ingress_postgres import draft

    submission_key = uuid4()
    submission = ProposalIngressService(database).submit(
        "archive-client", who, submission_key, draft()
    )
    before = read_durable_state_fingerprint(database)
    assert set(before.counts) == set(DURABLE_TABLES)
    assert all(before.counts[table] > 0 for table in DURABLE_TABLES)
    with psycopg.connect(database.database_url) as conn:
        schema = conn.execute("SELECT current_schema()").fetchone()[0]
    dump = subprocess.run(
        [
            "docker",
            "exec",
            container,
            "pg_dump",
            "-U",
            "knowledge_test",
            "-d",
            "knowledge_v32_test",
            "--format=custom",
            "--data-only",
            "--no-owner",
            "--no-privileges",
            *[f"--table={schema}.{table}" for table in DURABLE_TABLES],
        ],
        check=True,
        capture_output=True,
    )
    archive = dump.stdout
    assert archive
    warning = dump.stderr.decode()
    assert "circular foreign-key constraints" in warning
    assert "proposals" in warning and "proposal_reviews" in warning
    listing = subprocess.run(
        ["docker", "exec", "-i", container, "pg_restore", "--list"],
        input=archive,
        check=True,
        capture_output=True,
        text=False,
    ).stdout.decode()
    assert {line.split()[6] for line in listing.splitlines() if " TABLE DATA " in line} == set(
        DURABLE_TABLES
    )
    target = "knowledge_v32_restore_" + uuid4().hex
    failing_target = "knowledge_v32_restore_failure_" + uuid4().hex
    address = urlsplit(database.database_url)
    admin = urlunsplit(address._replace(query=""))
    restored_url = urlunsplit(address._replace(path="/" + target))
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target)))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(failing_target)))
    try:
        with psycopg.connect(restored_url, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION vector")
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            init_conversation_schema(conn)
        restore = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                container,
                "pg_restore",
                "-U",
                "knowledge_test",
                "-d",
                target,
                "--data-only",
                "--no-owner",
                "--no-privileges",
                "--single-transaction",
                "--exit-on-error",
            ],
            input=archive,
            check=False,
            capture_output=True,
        )
        assert restore.returncode == 0, restore.stderr.decode()
        restored = replace(database, database_url=restored_url)
        assert read_durable_state_fingerprint(restored) == before
        replay = ProposalIngressService(restored).submit(
            "archive-client", who, submission_key, draft()
        )
        assert not replay.created and replay.receipt == submission.receipt
        submitted_proposal = (
            PostgresReviewStore(restored).get(who, UUID(submission.receipt["proposal_id"])).proposal
        )
        assert submitted_proposal.proposed_content == draft().proposed_content
        context = PostgresReviewStore(restored).context(who, submitted_proposal.id)
        assert context[0].content == draft().provenance.original_text
        assert "model_output" in context[1].content
        assert PostgresReviewStore(restored).get(who, p.id).accepted_review_id == accepted.id
        assert (
            PostgresReviewStore(restored).get(deferred_who, deferred_proposal.id).proposal.status
            == "deferred"
        )
        with psycopg.connect(restored.database_url, autocommit=True) as conn:
            constraints = conn.execute(
                """SELECT conname, condeferrable, condeferred, convalidated
                FROM pg_constraint WHERE contype='f' ORDER BY conname"""
            ).fetchall()
            assert all(row[3] for row in constraints)
            deferred_fks = [(row[0], row[2]) for row in constraints if row[1]]
            assert deferred_fks == [("proposals_accepted_review_fk", True)]
            triggers = dict(
                conn.execute(
                    """SELECT tgname, tgenabled FROM pg_trigger
                    WHERE tgname IN ('proposal_reviews_immutable',
                                     'proposal_decisions_immutable', 'proposals_terminal',
                                     'proposal_applies_guard', 'proposal_applies_no_delete')"""
                ).fetchall()
            )
            assert triggers == {
                "proposal_reviews_immutable": "O",
                "proposal_decisions_immutable": "O",
                "proposals_terminal": "O",
                "proposal_applies_guard": "O",
                "proposal_applies_no_delete": "O",
            }
            with pytest.raises(psycopg.Error, match="Immutable review audit record"):
                conn.execute(
                    "UPDATE proposal_reviews SET new_content='tampered' WHERE id=%s",
                    (accepted.id,),
                )
            with pytest.raises(psycopg.Error, match="Terminal proposal is immutable"):
                conn.execute("UPDATE proposals SET title='tampered' WHERE id=%s", (p.id,))

        failing_url = urlunsplit(address._replace(path="/" + failing_target))
        with psycopg.connect(failing_url, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION vector")
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            init_conversation_schema(conn)
        restore_sql = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                container,
                "pg_restore",
                "--file=-",
                "--data-only",
                "--no-owner",
                "--no-privileges",
            ],
            input=archive,
            check=True,
            capture_output=True,
        ).stdout
        restore_lines = restore_sql.splitlines(keepends=True)
        assert sum(line.startswith(b"\\restrict ") for line in restore_lines) == 1
        assert sum(line.startswith(b"\\unrestrict ") for line in restore_lines) == 1
        sequence_lines = [line for line in restore_lines if b"pg_catalog.setval(" in line]
        assert len(sequence_lines) == 1
        restore_without_setval = b"".join(
            line for line in restore_lines if b"pg_catalog.setval(" not in line
        )
        final_failure = f"""
            ALTER SEQUENCE {schema}.messages_id_seq RESTART WITH 999;
            DO $$ BEGIN RAISE EXCEPTION 'synthetic final restore failure'; END $$;
            """.encode()
        with pytest.raises(subprocess.CalledProcessError):
            subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    container,
                    "psql",
                    "-U",
                    "knowledge_test",
                    "-d",
                    failing_target,
                    "--set=ON_ERROR_STOP=1",
                    "--single-transaction",
                    "--file=-",
                ],
                input=restore_without_setval + final_failure,
                check=True,
                capture_output=True,
            )
        failed = replace(database, database_url=failing_url)
        with psycopg.connect(failed.database_url) as conn:
            counts = [
                conn.execute(
                    sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
                ).fetchone()[0]
                for table in DURABLE_TABLES
            ]
            trigger_enabled = conn.execute(
                "SELECT tgenabled FROM pg_trigger WHERE tgname='proposal_reviews_immutable'"
            ).fetchone()[0]
            sequence_state = conn.execute(
                "SELECT last_value, is_called FROM messages_id_seq"
            ).fetchone()
        assert counts == [0] * len(DURABLE_TABLES)
        assert trigger_enabled == "O"
        assert sequence_state == (1, False)
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(target)))
            conn.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(failing_target)))
