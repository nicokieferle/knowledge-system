"""Real browser-to-domain-to-PostgreSQL gate, isolated by the V3.2 fixture."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import replace
from threading import Barrier

import psycopg
import pytest
from fastapi.testclient import TestClient

from knowledge_system.conversation_models import MessageRole, ProposalDraft, ProposalTriggerType
from knowledge_system.conversation_store import PostgresConversationStore
from knowledge_system.db import init_db
from knowledge_system.indexer import index_document
from knowledge_system.knowledge_apply import SecureKnowledgeWriter
from knowledge_system.proposal_apply import PostgresApplyStore, ProposalApplyService
from knowledge_system.proposal_review import ProposalStatus
from knowledge_system.proposal_store import PostgresProposalStore, ProposalCreate
from knowledge_system.review_web import create_app
from tests.integration import test_proposal_review_postgres as pg_review
from tests.integration.test_proposal_review_postgres import initialize, seed
from tests.test_review_web import login, web_settings


@pytest.fixture
def database(tmp_path):
    yield from pg_review.database.__wrapped__(tmp_path)


def application(settings):
    return ProposalApplyService(settings, document_indexer=lambda document: None)


class FakeEmbedder:
    def encode(self, values):
        return [[float(index + 1)] * 384 for index, _ in enumerate(values)]


def real_document_indexer(settings):
    return lambda document: index_document(
        settings, document, embedder=FakeEmbedder(), coordinated=True
    )


class SimulatedProcessLoss(BaseException):
    pass


@pytest.mark.parametrize(
    "first,last",
    [
        ("pending", "accept-apply"),
        ("pending", "reject"),
        ("pending", "defer"),
        ("deferred", "accept-apply"),
        ("deferred", "reject"),
    ],
)
def test_browser_lifecycle_and_retry_exact_revision(database, first, last):
    initialize(database)
    service, who, p = seed(database)
    settings = replace(web_settings(), owner=who)
    app = create_app(settings, service, application(database))
    with TestClient(app) as client:
        token = login(client)
        path = f"/reviews/{p.id}"
        assert client.get(path).status_code == 200
        assert service.get(who, p.id).revision is None
        assert (
            client.post(path + "/refresh", data={"csrf": token}, follow_redirects=False).status_code
            == 303
        )
        r = service.get(who, p.id).revision
        data = {"csrf": token, "review_id": str(r.id), "status": "pending", "confirmed": "yes"}
        if first == "deferred":
            assert (
                client.post(path + "/decision/defer", data=data, follow_redirects=False).status_code
                == 303
            )
            data["status"] = "deferred"
        for _ in range(2):
            assert (
                client.post(
                    path + "/decision/" + last, data=data, follow_redirects=False
                ).status_code
                == 303
            )
        if last == "accept-apply":
            assert service.get(who, p.id).accepted_review_id == r.id
            assert (database.knowledge_root / "new.md").read_text() == "new\n"
        with psycopg.connect(database.database_url) as conn:
            assert conn.execute("SELECT count(*) FROM proposal_decisions").fetchone()[0] == (
                2 if first == "deferred" else 1
            )


@pytest.mark.parametrize(
    "actions",
    [("accept-apply", "reject"), ("defer", "accept-apply"), ("defer", "reject")],
)
def test_competing_browser_tabs_use_original_status_cas(database, actions):
    initialize(database)
    service, who, p = seed(database)
    r = service.prepare(who, p.id).revision
    app = create_app(replace(web_settings(), owner=who), service, application(database))
    barrier = Barrier(2)

    def run(action):
        with TestClient(app) as client:
            token = login(client)
            barrier.wait(timeout=10)
            return client.post(
                f"/reviews/{p.id}/decision/{action}",
                data={
                    "csrf": token,
                    "review_id": str(r.id),
                    "status": "pending",
                    "confirmed": "yes",
                },
                follow_redirects=False,
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, actions))
    assert sorted(results) == [303, 409]
    with psycopg.connect(database.database_url) as conn:
        assert conn.execute("SELECT count(*) FROM proposal_decisions").fetchone()[0] == 1


def test_web_decision_waits_for_proposal_row_lock(database):
    initialize(database)
    service, who, p = seed(database)
    r = service.prepare(who, p.id).revision
    with TestClient(
        create_app(replace(web_settings(), owner=who), service, application(database))
    ) as client:
        token = login(client)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with psycopg.connect(database.database_url) as conn:
                conn.execute("SELECT id FROM proposals WHERE id=%s FOR UPDATE", (p.id,))
                future = pool.submit(
                    client.post,
                    f"/reviews/{p.id}/decision/reject",
                    data={
                        "csrf": token,
                        "review_id": str(r.id),
                        "status": "pending",
                        "confirmed": "yes",
                    },
                    follow_redirects=False,
                )
                with pytest.raises(TimeoutError):
                    future.result(timeout=0.3)
                conn.commit()
            assert future.result(timeout=10).status_code == 303


def test_browser_conflict_refresh_is_post_only_and_redirects_to_successor(database):
    initialize(database)
    service, who, proposal = seed(database)
    revision = service.prepare(who, proposal.id).revision
    (database.knowledge_root / "new.md").write_text("concurrent\n", encoding="utf-8")
    app = create_app(replace(web_settings(), owner=who), service, application(database))
    with TestClient(app) as client:
        token = login(client)
        path = f"/reviews/{proposal.id}"
        accepted = client.post(
            path + "/decision/accept-apply",
            data={
                "csrf": token,
                "review_id": str(revision.id),
                "status": "pending",
                "confirmed": "yes",
            },
            follow_redirects=False,
        )
        assert accepted.status_code == 303
        page = client.get(path)
        assert "basis_changed" in page.text and "Refresh review" in page.text
        assert client.get(path + "/refresh-review").status_code == 405
        refreshed = client.post(
            path + "/refresh-review", data={"csrf": token}, follow_redirects=False
        )
        assert refreshed.status_code == 303
        assert refreshed.headers["location"] != path
        successor = client.get(refreshed.headers["location"])
        assert successor.status_code == 200
        assert "concurrent" in successor.text and "Accept &amp; Apply" in successor.text


def test_browser_legacy_apply_and_index_retry_show_real_states(database):
    initialize(database)
    service, who, proposal = seed(database)
    revision = service.prepare(who, proposal.id).revision
    service.decide(who, proposal.id, revision.id, ProposalStatus.ACCEPTED)
    apply_service = application(database)
    app = create_app(replace(web_settings(), owner=who), service, apply_service)
    with TestClient(app) as client:
        token = login(client)
        path = f"/reviews/{proposal.id}"
        legacy = client.get(path)
        assert "Akzeptiert, noch nicht angewendet" in legacy.text
        applied = client.post(
            path + "/apply",
            data={"csrf": token, "review_id": str(revision.id)},
            follow_redirects=False,
        )
        assert applied.status_code == 303
        page = client.get(path)
        assert "<dd>applied</dd>" in page.text and "<dd>indexed</dd>" in page.text

    other_service, other_who, other = seed(database)
    other_revision = other_service.prepare(
        other_who, other.id, target_source_path="other.md"
    ).revision

    def fail_index(_document):
        raise RuntimeError("private failure detail")

    failing_apply = ProposalApplyService(database, document_indexer=fail_index)
    failing_app = create_app(replace(web_settings(), owner=other_who), other_service, failing_apply)
    with TestClient(failing_app) as client:
        token = login(client)
        path = f"/reviews/{other.id}"
        response = client.post(
            path + "/decision/accept-apply",
            data={
                "csrf": token,
                "review_id": str(other_revision.id),
                "status": "pending",
                "confirmed": "yes",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        page = client.get(path)
        assert "index_operation_failed" in page.text and "Retry indexing" in page.text
        assert "private failure detail" not in page.text
        failing_apply.document_indexer = lambda document: None
        retried = client.post(path + "/retry-index", data={"csrf": token}, follow_redirects=False)
        assert retried.status_code == 303
        assert "<dd>indexed</dd>" in client.get(path).text


@pytest.mark.parametrize("after_index_commit", [False, True])
def test_browser_recovers_pending_index_through_authenticated_csrf_post(
    database, after_index_commit
):
    init_db(database)
    reviews, who, proposal = seed(database)
    revision = reviews.prepare(who, proposal.id).revision
    store = PostgresApplyStore(database)
    store.accept_and_intent(who, proposal.id, revision.id, "pending")
    store.execute_apply(who, proposal.id, SecureKnowledgeWriter(database.knowledge_root))

    if after_index_commit:

        def committed_then_process_loss(document):
            index_document(database, document, embedder=FakeEmbedder(), coordinated=True)
            raise SimulatedProcessLoss()

        with pytest.raises(SimulatedProcessLoss):
            store.execute_index(
                who,
                proposal.id,
                reviews.source,
                committed_then_process_loss,
            )

    apply_service = ProposalApplyService(
        database, store=store, document_indexer=real_document_indexer(database)
    )
    app = create_app(replace(web_settings(), owner=who), reviews, apply_service)
    with TestClient(app) as client:
        token = login(client)
        path = f"/reviews/{proposal.id}"
        page = client.get(path)
        assert "<dd>pending</dd>" in page.text
        assert "Retry indexing" in page.text
        assert client.post(path + "/retry-index", data={"csrf": "wrong"}).status_code == 403
        retried = client.post(path + "/retry-index", data={"csrf": token}, follow_redirects=False)
        assert retried.status_code == 303
        completed = client.get(path)
        assert "<dd>indexed</dd>" in completed.text
        assert "Retry indexing" not in completed.text

    with psycopg.connect(database.database_url) as conn:
        assert (
            conn.execute("SELECT count(*) FROM chunks WHERE source_path='new.md'").fetchone()[0]
            == 1
        )


def test_browser_pending_index_retry_rejects_unapplied_and_foreign_owner(database):
    initialize(database)
    reviews, who, proposal = seed(database)
    revision = reviews.prepare(who, proposal.id).revision
    store = PostgresApplyStore(database)
    store.accept_and_intent(who, proposal.id, revision.id, "pending")
    apply_service = ProposalApplyService(database, store=store, document_indexer=lambda _: None)

    with TestClient(
        create_app(replace(web_settings(), owner=who), reviews, apply_service)
    ) as client:
        token = login(client)
        response = client.post(
            f"/reviews/{proposal.id}/retry-index",
            data={"csrf": token},
            follow_redirects=False,
        )
        assert response.status_code == 409

    foreign = replace(who, external_user_id="foreign")
    with TestClient(
        create_app(replace(web_settings(), owner=foreign), reviews, apply_service)
    ) as client:
        token = login(client)
        response = client.post(
            f"/reviews/{proposal.id}/retry-index",
            data={"csrf": token},
            follow_redirects=False,
        )
        assert response.status_code == 404


def test_queue_over_100_owner_scope_and_context(database):
    initialize(database)
    service, who, p = seed(database)
    convs = PostgresConversationStore(database)
    stores = PostgresProposalStore(database)
    origin = convs.add_message(
        p.conversation_id, MessageRole.USER, "synthetic original <script>not html</script>"
    )
    for i in range(104):
        trigger = convs.add_message(p.conversation_id, MessageRole.USER, f"control {i}")
        stores.create_proposal(
            ProposalCreate(
                p.conversation_id,
                "test",
                ProposalTriggerType.COMMAND,
                trigger.id,
                (origin.id,),
                ProposalDraft(
                    "summary",
                    "reason",
                    "new",
                    target_source_id="knowledge-git",
                    target_source_path="notes.md",
                ),
            )
        )
    own = service.queue(who, ("pending", "deferred"), 0, 30)
    assert own.total == own.open_count == 105
    ids = []
    for offset in range(0, 105, 30):
        ids.extend(item.id for item in service.queue(who, ("pending",), offset, 30).items)
    assert len(ids) == len(set(ids)) == 105
    assert service.context(who, ids[0])[0].content == origin.content
    other = replace(who, external_user_id="other")
    assert service.queue(other, ("pending",), 0, 30).total == 0
    with TestClient(
        create_app(replace(web_settings(), owner=other), service, application(database))
    ) as client:
        token = login(client)
        assert client.get(f"/reviews/{p.id}").status_code == 404
        assert client.post(f"/reviews/{p.id}/refresh", data={"csrf": token}).status_code == 404
    assert service.get(who, p.id).revision is None
