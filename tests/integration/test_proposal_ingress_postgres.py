"""Real atomic submission, concurrency and owner/browser gate on isolated PostgreSQL."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from knowledge_system.client_state import ClientIdentity, PostgresClientStateStore
from knowledge_system.conversation_store import PostgresConversationStore
from knowledge_system.db import init_db
from knowledge_system.proposal_ingress import ProposalIngressService, parse_submission
from knowledge_system.proposal_ingress_web import create_app
from knowledge_system.proposal_review import ProposalReviewService, ProposalStatus, ReviewForbidden
from knowledge_system.review_store import PostgresReviewStore
from knowledge_system.review_web import create_app as review_app
from knowledge_system.sources import GitMarkdownSource
from tests.integration import test_proposal_review_postgres as pg_review
from tests.test_proposal_ingress import OWNER, TOKEN, headers, payload, settings
from tests.test_review_web import login, web_settings


@pytest.fixture
def database(tmp_path):
    yield from pg_review.database.__wrapped__(tmp_path)


def draft():
    return parse_submission(json.dumps(payload()).encode())


def counts(database):
    with psycopg.connect(database.database_url) as conn:
        return tuple(
            conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "conversations",
                "client_states",
                "client_conversations",
                "messages",
                "proposals",
                "proposal_reviews",
                "proposal_decisions",
                "proposal_applies",
                "chunks",
            )
        )


def test_http_submission_browser_provenance_owner_and_explicit_prepare(database):
    init_db(database)
    submitter = ProposalIngressService(database)
    key = uuid4()
    with TestClient(create_app(settings(), submitter)) as client:
        first = client.post("/v1/proposals", json=payload(), headers=headers(key))
        assert first.status_code == 201
        receipt = first.json()
        replay = client.post("/v1/proposals", json=payload(), headers=headers(key))
        assert replay.status_code == 200 and replay.json() == receipt
        denied = client.post(
            "/v1/proposals",
            json=payload(),
            headers={"Authorization": "Bearer foreign", "Idempotency-Key": str(uuid4())},
        )
        assert denied.status_code == 401
        injected = client.post(
            "/v1/proposals", json=payload() | {"owner": "foreign"}, headers=headers()
        )
        assert injected.status_code == 422
    assert counts(database) == (1, 1, 1, 3, 1, 0, 0, 0, 0)
    pid, cid = UUID(receipt["proposal_id"]), UUID(receipt["conversation_id"])
    reviews = ProposalReviewService(
        PostgresReviewStore(database), GitMarkdownSource(database.knowledge_root)
    )
    view = reviews.get(OWNER, pid)
    assert view.proposal.status == "pending" and view.revision is None
    assert view.proposal.source_client == settings().client_id
    assert view.proposal.base_revision is None
    assert view.proposal.proposed_content == payload()["proposed_content"]
    messages = PostgresConversationStore(database).list_messages(cid)
    assert messages[0].content == payload()["provenance"]["original_text"]
    assert view.proposal.originating_message_ids == (messages[0].id, messages[1].id)
    assert view.proposal.trigger_message_id == messages[2].id
    assert messages[2].metadata == {
        "control": "external_submission",
        "client_id": settings().client_id,
        "owner": {"client_type": "test", "external_chat_id": "chat", "external_user_id": "user"},
        "fingerprint": draft().fingerprint,
        "receipt": receipt,
    }
    browser = review_app(replace(web_settings(), owner=OWNER), reviews)
    with TestClient(browser) as client:
        # Submit credential does not establish a browser session or CSRF authority.
        assert (
            client.post(
                f"/reviews/{pid}/refresh",
                headers={"Authorization": "Bearer " + TOKEN},
                follow_redirects=False,
            ).status_code
            == 401
        )
        csrf = login(client)
        assert str(pid) in client.get("/reviews").text
        page = client.get(f"/reviews/{pid}")
        assert page.status_code == 200
        assert "Ursprünglich" in page.text and "model_output" in page.text
        assert "hypothesis" in page.text and settings().client_id in page.text
        assert "&lt;script&gt;" in page.text and "<script>alert(1)</script>" not in page.text
        assert "ungeprüfte Clientangaben" in page.text
        assert counts(database) == (1, 1, 1, 3, 1, 0, 0, 0, 0)
        assert not list(database.knowledge_root.rglob("*.md"))
        assert (
            client.post(
                f"/reviews/{pid}/refresh", data={"csrf": csrf}, follow_redirects=False
            ).status_code
            == 303
        )
    assert reviews.get(OWNER, pid).revision is not None
    foreign = ClientIdentity("test", "chat", "foreign")
    assert reviews.queue(foreign, ("pending",)).total == 0
    for operation in (reviews.get, reviews.context):
        with pytest.raises(ReviewForbidden):
            operation(foreign, pid)
    with TestClient(review_app(replace(web_settings(), owner=foreign), reviews)) as client:
        login(client)
        assert str(pid) not in client.get("/reviews").text
        assert client.get(f"/reviews/{pid}").status_code == 404
        assert "Ursprünglich" not in client.get(f"/reviews/{pid}").text


def test_serial_parallel_restart_response_loss_and_terminal_replay(database):
    init_db(database)
    key = uuid4()
    barrier = Barrier(8)

    def submit(_):
        barrier.wait(timeout=10)
        # Independent app/service instances simulate process boundaries.
        with TestClient(create_app(settings(), ProposalIngressService(database))) as client:
            return client.post("/v1/proposals", json=payload(), headers=headers(key))

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(submit, range(8)))
    assert sorted(r.status_code for r in responses) == [200] * 7 + [201]
    receipt = responses[0].json()
    assert all(r.json() == receipt for r in responses)
    assert counts(database) == (1, 1, 1, 3, 1, 0, 0, 0, 0)
    reviews = ProposalReviewService(
        PostgresReviewStore(database), GitMarkdownSource(database.knowledge_root)
    )
    pid = UUID(receipt["proposal_id"])
    revision = reviews.prepare(OWNER, pid).revision
    reviews.decide(OWNER, pid, revision.id, ProposalStatus.REJECTED)
    before = counts(database)
    # Rotation preserves the machine principal; receipt remains historical after review.
    rotated = replace(settings(), token="rotated-synthetic-credential-1234567890")
    with TestClient(create_app(rotated, ProposalIngressService(database))) as client:
        response = client.post(
            "/v1/proposals",
            json=payload(),
            headers={"Authorization": "Bearer " + rotated.token, "Idempotency-Key": str(key)},
        )
        assert response.status_code == 200 and response.json() == receipt
    assert reviews.get(OWNER, pid).proposal.status == "rejected"
    assert counts(database) == before
    # Commit then lost response: caller throws away result and retries a new service.
    lost_key = uuid4()
    ProposalIngressService(database).submit(settings().client_id, OWNER, lost_key, draft())
    repeated = ProposalIngressService(database).submit(
        settings().client_id, OWNER, lost_key, draft()
    )
    assert not repeated.created and counts(database)[0] == 2


def test_conflicts_and_separate_machine_namespaces(database):
    init_db(database)
    service = ProposalIngressService(database)
    key = uuid4()
    original = service.submit(settings().client_id, OWNER, key, draft())
    before = counts(database)
    with TestClient(create_app(settings(), service)) as client:
        response = client.post(
            "/v1/proposals",
            json=payload() | {"reason": "Changed search result"},
            headers=headers(key),
        )
        assert response.status_code == 409 and response.json()["error"] == "idempotency_conflict"
        assert original.receipt["proposal_id"] not in response.text
    changed_owner = replace(settings(), owner=ClientIdentity("test", "chat", "other"))
    with TestClient(create_app(changed_owner, service)) as client:
        response = client.post("/v1/proposals", json=payload(), headers=headers(key))
        assert response.status_code == 409 and response.json()["error"] == "binding_conflict"
    assert counts(database) == before
    other = service.submit("another-client", OWNER, key, draft())
    assert other.created and other.receipt != original.receipt


def test_parallel_payload_conflict_has_one_winner(database):
    init_db(database)
    key = uuid4()
    barrier = Barrier(2)

    def submit(reason):
        barrier.wait(timeout=10)
        with TestClient(create_app(settings(), ProposalIngressService(database))) as client:
            return client.post(
                "/v1/proposals", json=payload() | {"reason": reason}, headers=headers(key)
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, ("First search", "Second search")))
    assert sorted(r.status_code for r in responses) == [201, 409]
    assert counts(database) == (1, 1, 1, 3, 1, 0, 0, 0, 0)


@pytest.mark.parametrize("table", ["client_conversations", "messages", "proposals"])
def test_database_failure_rolls_back_every_component(database, table):
    init_db(database)
    with psycopg.connect(database.database_url, autocommit=True) as conn:
        conn.execute("""CREATE FUNCTION reject_submission() RETURNS trigger LANGUAGE plpgsql
        AS $$ BEGIN RAISE EXCEPTION 'synthetic private failure'; END $$""")
        conn.execute(
            f"CREATE TRIGGER reject_submission BEFORE INSERT ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_submission()"
        )
    key = uuid4()
    with TestClient(create_app(settings(), ProposalIngressService(database))) as client:
        response = client.post("/v1/proposals", json=payload(), headers=headers(key))
        assert response.status_code == 503 and "private" not in response.text
        assert counts(database) == (0, 0, 0, 0, 0, 0, 0, 0, 0)
        with psycopg.connect(database.database_url, autocommit=True) as conn:
            conn.execute(f"DROP TRIGGER reject_submission ON {table}")
        assert client.post("/v1/proposals", json=payload(), headers=headers(key)).status_code == 201


def test_archived_audits_preserve_active_topic_and_paginated_queue(database):
    init_db(database)
    conversations = PostgresConversationStore(database)
    states = PostgresClientStateStore(database)
    topic = conversations.create_conversation("test", "Active synthetic topic")
    active = states.activate(OWNER, topic.id)
    service = ProposalIngressService(database)
    receipts = [
        service.submit(settings().client_id, OWNER, uuid4(), draft()).receipt for _ in range(33)
    ]
    assert states.get(OWNER) == active
    assert [c.id for c in states.list_conversations(OWNER)] == [topic.id]
    assert [c.id for c in conversations.list_conversations()] == [topic.id]
    assert all(
        conversations.get_conversation(UUID(r["conversation_id"])).archived_at for r in receipts
    )
    reviews = ProposalReviewService(
        PostgresReviewStore(database), GitMarkdownSource(database.knowledge_root)
    )
    with TestClient(review_app(replace(web_settings(), owner=OWNER), reviews)) as client:
        login(client)
        pages = [client.get("/reviews?page=" + str(p)).text for p in (1, 2)]
        assert all(any(r["proposal_id"] in p for p in pages) for r in receipts)
        assert any(r["proposal_id"] in pages[1] for r in receipts)
    assert counts(database)[5:] == (0, 0, 0, 0)
