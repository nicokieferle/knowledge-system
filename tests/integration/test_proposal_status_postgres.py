"""Own ingress status on real isolated PostgreSQL; synthetic data and HTTP TestClient."""

from contextlib import contextmanager
from dataclasses import replace
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from knowledge_system.client_state import ClientIdentity
from knowledge_system.knowledge_apply import KnowledgeIOFailure
from knowledge_system.proposal_apply import ProposalApplyService
from knowledge_system.proposal_ingress import ProposalIngressService
from knowledge_system.proposal_ingress_web import create_app
from knowledge_system.proposal_review import ProposalReviewService, ProposalStatus
from knowledge_system.review_store import PostgresReviewStore
from knowledge_system.sources import GitMarkdownSource
from tests.integration import test_proposal_review_postgres as pg_review
from tests.integration.test_proposal_ingress_postgres import draft
from tests.integration.test_review_web_postgres import real_document_indexer
from tests.test_proposal_ingress import OWNER, headers, payload, settings


@pytest.fixture
def database(tmp_path):
    yield from pg_review.database.__wrapped__(tmp_path)


def snapshot(database):
    # Compare full persisted rows, not just counts, and all canonical bytes.
    tables = (
        "conversations",
        "messages",
        "conversation_summaries",
        "proposal_suggestions",
        "proposals",
        "client_states",
        "client_conversations",
        "client_message_bindings",
        "proposal_reviews",
        "proposal_decisions",
        "proposal_applies",
        "chunks",
        "index_metadata",
    )
    with psycopg.connect(database.database_url) as conn:
        rows = tuple(
            sorted(conn.execute(f"SELECT to_jsonb(t)::text FROM {table} t").fetchall())
            for table in tables
        )
    files = {
        str(p.relative_to(database.knowledge_root)): p.read_bytes()
        for p in database.knowledge_root.rglob("*")
        if p.is_file()
    }
    return rows, files


def poll(database, pid, config=None, expected_code=200):
    config = config or settings()
    before = snapshot(database)
    with TestClient(create_app(config, ProposalIngressService(database))) as client:
        response = client.get(
            f"/v1/proposals/{pid}/status", headers={"Authorization": "Bearer " + config.token}
        )
    assert response.status_code == expected_code
    assert snapshot(database) == before
    if expected_code == 200:
        assert set(response.json()) == {
            "proposal_id",
            "proposal_status",
            "accepted_review_id",
            "apply_status",
            "index_status",
        }
        assert response.json()["proposal_id"] == str(pid)
    else:
        assert response.json()["error"] == "not_found"
        assert set(response.json()) == {"error", "correlation_id"}
    return response.json()


def submit(database):
    from knowledge_system.db import init_db

    init_db(database)
    body = payload() | {"target_source_path": "example.md"}
    key = uuid4()
    with TestClient(create_app(settings(), ProposalIngressService(database))) as client:
        response = client.post("/v1/proposals", json=body, headers=headers(key))
    assert response.status_code == 201
    reviews = ProposalReviewService(
        PostgresReviewStore(database), GitMarkdownSource(database.knowledge_root)
    )
    return UUID(response.json()["proposal_id"]), reviews, key, body, response.json()


def test_status_pending_prepared_deferred_rejected_and_rotated_replay(database):
    pid, reviews, key, body, receipt = submit(database)
    expected = {
        "proposal_id": str(pid),
        "proposal_status": "pending",
        "accepted_review_id": None,
        "apply_status": None,
        "index_status": None,
    }
    assert poll(database, pid) == expected
    revision = reviews.prepare(OWNER, pid).revision
    assert poll(database, pid) == expected
    reviews.decide(OWNER, pid, revision.id, ProposalStatus.DEFERRED)
    assert poll(database, pid) == expected | {"proposal_status": "deferred"}
    reviews.decide(OWNER, pid, revision.id, ProposalStatus.REJECTED, expected_status="deferred")
    rotated = replace(settings(), token="rotated-synthetic-credential-1234567890")
    assert poll(database, pid, rotated) == expected | {"proposal_status": "rejected"}
    before = snapshot(database)
    with TestClient(create_app(rotated, ProposalIngressService(database))) as client:
        assert client.get(f"/v1/proposals/{pid}/status", headers=headers()).status_code == 401
        replay = client.post(
            "/v1/proposals",
            json=body,
            headers={"Authorization": "Bearer " + rotated.token, "Idempotency-Key": str(key)},
        )
        assert replay.status_code == 200 and replay.json() == receipt
    assert snapshot(database) == before


@pytest.mark.parametrize("outcome", ["applied", "conflict", "failed"])
def test_status_accepted_without_intent_and_separate_apply_index_phases(database, outcome):
    pid, reviews, _, _, _ = submit(database)
    revision = reviews.prepare(
        OWNER, pid, new_content="# Human correction\n\nSynthetic.\n"
    ).revision
    reviews.decide(OWNER, pid, revision.id, ProposalStatus.ACCEPTED)
    expected = {
        "proposal_id": str(pid),
        "proposal_status": "accepted",
        "accepted_review_id": str(revision.id),
        "apply_status": None,
        "index_status": None,
    }
    assert poll(database, pid) == expected
    service = ProposalApplyService(database, document_indexer=real_document_indexer(database))
    service.store.ensure_intent(OWNER, pid, revision.id)
    assert poll(database, pid) == expected | {"apply_status": "pending", "index_status": "pending"}
    writer = service.writer
    if outcome == "conflict":
        (database.knowledge_root / "example.md").write_bytes(b"Third state\n")
    elif outcome == "failed":
        # Only the writer is replaced to inject a deterministic I/O failure.
        class FailingWriter:
            def apply(self, *args):
                raise KnowledgeIOFailure("synthetic_io_failure")

        writer = FailingWriter()
    result = service.store.execute_apply(OWNER, pid, writer)
    assert result.apply.apply_status == outcome
    assert poll(database, pid) == expected | {"apply_status": outcome, "index_status": "pending"}
    if outcome == "conflict":
        successor = service.refresh_review(OWNER, pid).proposal
        # It shares owner, conversation and source_client, but not the original receipt/trigger.
        poll(database, successor.id, expected_code=404)
        assert poll(database, pid)["apply_status"] == "conflict"
    elif outcome == "applied":

        def failed_index(document, *, coordination):
            raise RuntimeError("synthetic index failure")

        service.store.execute_index(OWNER, pid, service.source, failed_index)
        assert poll(database, pid) == expected | {
            "apply_status": "applied",
            "index_status": "failed",
        }
        service.retry_index(OWNER, pid)
        assert poll(database, pid) == expected | {
            "apply_status": "applied",
            "index_status": "indexed",
        }
        assert (
            database.knowledge_root / "example.md"
        ).read_bytes() == revision.content.new_content.encode()


def test_status_requires_original_machine_receipt_owner_and_live_ownership(database):
    pid, _, _, _, _ = submit(database)
    service = ProposalIngressService(database)
    other = service.submit("other-machine", OWNER, uuid4(), draft())
    poll(database, UUID(other.receipt["proposal_id"]), expected_code=404)
    poll(database, pid, replace(settings(), client_id="other-machine"), expected_code=404)
    poll(database, uuid4(), expected_code=404)
    foreign = ClientIdentity("test", "chat", "other-owner")
    # Even granting live ownership to the new owner must not replace the saved delegation.
    with psycopg.connect(database.database_url) as conn:
        conn.execute(
            "INSERT INTO client_states(client_type,external_chat_id,external_user_id) VALUES (%s,%s,%s)",
            (foreign.client_type, foreign.external_chat_id, foreign.external_user_id),
        )
        conn.execute(
            """INSERT INTO client_conversations(client_type,external_chat_id,external_user_id,conversation_id)
            SELECT %s,%s,%s,conversation_id FROM proposals WHERE id=%s""",
            (foreign.client_type, foreign.external_chat_id, foreign.external_user_id, pid),
        )
    poll(database, pid, replace(settings(), owner=foreign), expected_code=404)
    assert poll(database, pid)["proposal_status"] == "pending"
    with psycopg.connect(database.database_url) as conn:
        conn.execute(
            "DELETE FROM client_conversations WHERE external_user_id=%s", (OWNER.external_user_id,)
        )
    poll(database, pid, expected_code=404)


def test_status_uses_read_only_snapshot_across_concurrent_review_commit(database):
    pid, reviews, _, _, _ = submit(database)
    revision = reviews.prepare(OWNER, pid).revision

    class ConcurrentConnection:
        def __init__(self, conn):
            self.conn = conn

        def transaction(self):
            return self.conn.transaction()

        def execute(self, query, *args):
            cursor = self.conn.execute(query, *args)
            if query.startswith("SET TRANSACTION"):
                # Establish an actual PostgreSQL snapshot, then commit on another connection.
                assert (
                    self.conn.execute(
                        "SELECT status FROM proposals WHERE id=%s", (pid,)
                    ).fetchone()[0]
                    == "pending"
                )
                with pytest.raises(psycopg.errors.ReadOnlySqlTransaction), self.conn.transaction():
                    self.conn.execute("UPDATE proposals SET status=status WHERE id=%s", (pid,))
                reviews.decide(OWNER, pid, revision.id, ProposalStatus.ACCEPTED)
            return cursor

    @contextmanager
    def connection_factory(config):
        with psycopg.connect(config.database_url) as conn:
            yield ConcurrentConnection(conn)

    service = ProposalIngressService(database, connection_factory=connection_factory)
    result = service.status(settings().client_id, OWNER, pid)
    assert result["proposal_status"] == "pending" and result["accepted_review_id"] is None
    assert poll(database, pid)["accepted_review_id"] == str(revision.id)
