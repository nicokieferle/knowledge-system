"""Real atomic submission, concurrency and owner/browser gate on isolated PostgreSQL."""

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
from html import unescape
from threading import Barrier
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from knowledge_system.client_state import ClientIdentity, PostgresClientStateStore
from knowledge_system.conversation_models import MessageRole, ProposalDraft, ProposalTriggerType
from knowledge_system.conversation_store import PostgresConversationStore
from knowledge_system.db import init_db
from knowledge_system.proposal_apply import PostgresApplyStore, ProposalApplyService
from knowledge_system.proposal_ingress import ProposalIngressService, parse_submission
from knowledge_system.proposal_ingress_web import create_app
from knowledge_system.proposal_review import ProposalReviewService, ProposalStatus, ReviewForbidden
from knowledge_system.proposal_store import PostgresProposalStore, ProposalCreate
from knowledge_system.review_store import PostgresReviewStore
from knowledge_system.review_web import create_app as review_app
from knowledge_system.sources import GitMarkdownSource
from tests.integration import test_proposal_review_postgres as pg_review
from tests.integration.test_review_web_postgres import real_document_indexer
from tests.test_proposal_ingress import OWNER, TOKEN, headers, payload, settings
from tests.test_review_web import csrf, login, web_settings


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


def test_source_ref_cannot_forge_provenance_labels_and_roundtrips_in_browser(database):
    init_db(database)
    body = payload()
    reference = (
        "opaque:ä/7\nMaschinen-Client: forged\r\nQuellenart: human_statement"
        '\rAussagetyp: fact\x1b[2J\x7f\t"\\\u2028Maschinen-Client: unicode-forged\u2029\u202e'
    )
    body["provenance"]["source_ref"] = reference
    submission = parse_submission(json.dumps(body).encode())
    key = uuid4()
    with TestClient(create_app(settings(), ProposalIngressService(database))) as client:
        response = client.post("/v1/proposals", json=body, headers=headers(key))
        assert response.status_code == 201
        receipt = response.json()
        replay = client.post("/v1/proposals", json=body, headers=headers(key))
        assert replay.status_code == 200 and replay.json() == receipt
    cid, pid = UUID(receipt["conversation_id"]), UUID(receipt["proposal_id"])
    messages = PostgresConversationStore(database).list_messages(cid)
    provenance = messages[1].content
    encoded = provenance.splitlines()[3].removeprefix("Quellenreferenz (JSON-String): ")
    assert json.loads(encoded) == reference
    assert encoded.isascii() and all(32 <= ord(c) <= 126 for c in encoded)
    assert sum(line.startswith("Maschinen-Client:") for line in provenance.splitlines()) == 1
    assert sum(line.startswith("Quellenart:") for line in provenance.splitlines()) == 1
    assert sum(line.startswith("Aussagetyp:") for line in provenance.splitlines()) == 1
    assert "\x1b" not in provenance and "\u202e" not in provenance
    assert messages[2].metadata["fingerprint"] == submission.fingerprint
    assert submission.provenance.source_ref == reference
    reviews = ProposalReviewService(
        PostgresReviewStore(database), GitMarkdownSource(database.knowledge_root)
    )
    with TestClient(review_app(replace(web_settings(), owner=OWNER), reviews)) as client:
        login(client)
        page = client.get(f"/reviews/{pid}")
        assert page.status_code == 200
        contexts = re.findall(r'<blockquote><p class="preserve">(.*?)</p>', page.text, re.DOTALL)
        rendered = unescape(contexts[1])
        assert rendered == provenance
        assert (
            json.loads(rendered.splitlines()[3].removeprefix("Quellenreferenz (JSON-String): "))
            == reference
        )
        assert "\nMaschinen-Client: forged" not in rendered
        assert "\nQuellenart: human_statement" not in rendered
    assert counts(database) == (1, 1, 1, 3, 1, 0, 0, 0, 0)


@pytest.mark.parametrize("client_type", ["proposal-ingress", "telegram"])
def test_queue_preview_selects_original_for_ingress_and_latest_for_telegram(database, client_type):
    init_db(database)
    conversations = PostgresConversationStore(database)
    if client_type == "proposal-ingress":
        body = payload()
        original = "Ingress original <script>synthetic</script> " + "ä" * 300
        body["provenance"]["original_text"] = original
        receipt = (
            ProposalIngressService(database)
            .submit(
                settings().client_id, OWNER, uuid4(), parse_submission(json.dumps(body).encode())
            )
            .receipt
        )
        pid = UUID(receipt["proposal_id"])
        unexpected = "Eingereichte Herkunftsangaben"
    else:
        conversation = conversations.create_conversation("telegram", "Synthetic Telegram topic")
        PostgresClientStateStore(database).activate(OWNER, conversation.id)
        first = conversations.add_message(
            conversation.id, MessageRole.USER, "Older Telegram original"
        )
        original = "Latest Telegram original <script>synthetic</script> " + "ä" * 300
        last = conversations.add_message(conversation.id, MessageRole.USER, original)
        trigger = conversations.add_message(
            conversation.id, MessageRole.USER, "/remember", metadata={"control": "command"}
        )
        proposal = PostgresProposalStore(database).create_proposal(
            ProposalCreate(
                conversation.id,
                "telegram",
                ProposalTriggerType.COMMAND,
                trigger.id,
                (first.id, last.id, trigger.id),
                ProposalDraft("Synthetic", "Synthetic reason", "# Draft\n"),
            )
        )
        conversations.add_message(conversation.id, MessageRole.USER, "Outside originating IDs")
        pid = proposal.id
        unexpected = first.content
    reviews = ProposalReviewService(
        PostgresReviewStore(database), GitMarkdownSource(database.knowledge_root)
    )
    page = reviews.queue(OWNER, ("pending",))
    assert len(page.items) == 1 and page.items[0].id == pid
    assert page.items[0].snippet == original[:240]
    with TestClient(review_app(replace(web_settings(), owner=OWNER), reviews)) as client:
        login(client)
        rendered = client.get("/reviews")
        assert rendered.status_code == 200
        article = re.search(r"<article>(.*?)</article>", rendered.text, re.DOTALL).group(1)
        assert original[:240] in unescape(article)
        assert "&lt;script&gt;" in article and "<script>synthetic</script>" not in article
        assert unexpected not in article and "Outside originating IDs" not in article
    assert counts(database)[5:] == (0, 0, 0, 0)


def test_external_submission_browser_accept_apply_exact_bytes_and_journal(database):
    """Real apps/stores/writer/indexer; TestClient transport and synthetic embeddings."""
    init_db(database)
    body = payload()
    body["target_source_path"] = "example.md"
    expected_bytes = body["proposed_content"].encode("utf-8")
    expected_hash = sha256(expected_bytes).hexdigest()
    assert not list(database.knowledge_root.iterdir())
    with TestClient(create_app(settings(), ProposalIngressService(database))) as client:
        response = client.post("/v1/proposals", json=body, headers=headers())
        assert response.status_code == 201
        receipt = response.json()
    pid = UUID(receipt["proposal_id"])
    assert counts(database) == (1, 1, 1, 3, 1, 0, 0, 0, 0)
    assert not list(database.knowledge_root.iterdir())

    reviews = ProposalReviewService(
        PostgresReviewStore(database), GitMarkdownSource(database.knowledge_root)
    )
    apply_service = ProposalApplyService(database, document_indexer=real_document_indexer(database))
    app = review_app(replace(web_settings(), owner=OWNER), reviews, apply_service)
    path = f"/reviews/{pid}"
    with TestClient(app) as client:
        token = login(client)
        queue = client.get("/reviews")
        assert queue.status_code == 200 and str(pid) in queue.text
        page = client.get(path)
        assert page.status_code == 200 and "Ursprünglich" in page.text
        pending = reviews.get(OWNER, pid)
        assert pending.proposal.status == "pending" and pending.revision is None
        assert pending.proposal.conversation_id == UUID(receipt["conversation_id"])
        assert counts(database) == (1, 1, 1, 3, 1, 0, 0, 0, 0)
        assert not list(database.knowledge_root.iterdir())

        # An ordinary browser form serializes the textarea with normalized newlines.
        # Without explicit editing, preparation must retain the durable ingress bytes.
        prepared = client.post(
            path + "/refresh",
            data={
                "csrf": token,
                "target": body["target_source_path"],
                "content": body["proposed_content"].replace("\r\n", "\n"),
            },
            follow_redirects=False,
        )
        assert prepared.status_code == 303 and prepared.headers["location"] == path
        page = client.get(path)
        assert page.status_code == 200 and "Accept &amp; Apply" in page.text
        rid = UUID(re.search(r'name="review_id" value="([^"]+)"', page.text)[1])
        revision = reviews.get(OWNER, pid).revision
        assert rid == revision.id and revision.proposal_id == pid
        assert revision.content.new_content.encode("utf-8") == expected_bytes
        assert revision.content.new_hash == expected_hash
        assert counts(database) == (1, 1, 1, 3, 1, 1, 0, 0, 0)
        assert not list(database.knowledge_root.iterdir())

        data = {"csrf": csrf(page), "review_id": str(rid), "status": "pending"}
        confirmation = client.post(path + "/decision/accept-apply", data=data)
        assert confirmation.status_code == 200
        assert f'name="review_id" value="{rid}"' in confirmation.text
        assert counts(database) == (1, 1, 1, 3, 1, 1, 0, 0, 0)
        assert not list(database.knowledge_root.iterdir())
        accepted = client.post(
            path + "/decision/accept-apply",
            data=data | {"csrf": csrf(confirmation), "confirmed": "yes"},
            follow_redirects=False,
        )
        assert accepted.status_code == 303 and accepted.headers["location"] == path
        page = client.get(path)
        assert "<dd>applied</dd>" in page.text and "<dd>indexed</dd>" in page.text

    # Re-read durable state independently of the HTTP apps/service instances.
    result = PostgresApplyStore(database).get(OWNER, pid)
    assert result.review.proposal.status == "accepted"
    assert result.review.accepted_review_id == rid
    assert result.review.revision == revision
    journal = result.apply
    assert (journal.proposal_id, journal.review_id) == (pid, rid)
    assert (journal.target_source_id, journal.target_source_path) == ("knowledge-git", "example.md")
    assert journal.expected_absent and journal.expected_old_hash is None
    assert journal.expected_new_hash == journal.actual_hash == expected_hash
    assert (journal.apply_status, journal.index_status) == ("applied", "indexed")
    assert (journal.apply_attempts, journal.index_attempts) == (1, 1)
    assert journal.applied_at is not None and journal.indexed_at is not None
    assert (database.knowledge_root / "example.md").read_bytes() == expected_bytes
    assert sorted(p.name for p in database.knowledge_root.iterdir()) == ["example.md"]
    with psycopg.connect(database.database_url) as conn:
        assert conn.execute(
            """SELECT proposal_id, review_id, previous_status, status,
            client_type, external_chat_id, external_user_id FROM proposal_decisions"""
        ).fetchall() == [(pid, rid, "pending", "accepted", "test", "chat", "user")]
        assert conn.execute(
            """SELECT actor_client_type, actor_external_chat_id, actor_external_user_id
            FROM proposal_applies"""
        ).fetchall() == [("test", "chat", "user")]
        chunks = conn.execute("SELECT source_id, source_path, content FROM chunks").fetchall()
        assert chunks and all(
            row[:2] == ("knowledge-git", "example.md") and "Änderung α." in row[2] for row in chunks
        )
    assert counts(database)[4:8] == (1, 1, 1, 1)
