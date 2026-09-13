"""Real browser-to-domain-to-PostgreSQL gate, isolated by the V3.2 fixture."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import replace
from threading import Barrier

import psycopg
import pytest
from fastapi.testclient import TestClient

from knowledge_system.conversation_models import MessageRole, ProposalDraft, ProposalTriggerType
from knowledge_system.conversation_store import PostgresConversationStore
from knowledge_system.proposal_store import PostgresProposalStore, ProposalCreate
from knowledge_system.review_web import create_app
from tests.integration import test_proposal_review_postgres as pg_review
from tests.integration.test_proposal_review_postgres import initialize, seed
from tests.test_review_web import login, web_settings


@pytest.fixture
def database(tmp_path):
    yield from pg_review.database.__wrapped__(tmp_path)


@pytest.mark.parametrize(
    "first,last",
    [
        ("pending", "accept"),
        ("pending", "reject"),
        ("pending", "defer"),
        ("deferred", "accept"),
        ("deferred", "reject"),
    ],
)
def test_browser_lifecycle_and_retry_exact_revision(database, first, last):
    initialize(database)
    service, who, p = seed(database)
    settings = replace(web_settings(), owner=who)
    app = create_app(settings, service)
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
                client.post(path + "/defer", data=data, follow_redirects=False).status_code == 303
            )
            data["status"] = "deferred"
        for _ in range(2):
            assert (
                client.post(path + "/" + last, data=data, follow_redirects=False).status_code == 303
            )
        if last == "accept":
            assert service.get(who, p.id).accepted_review_id == r.id
        with psycopg.connect(database.database_url) as conn:
            assert conn.execute("SELECT count(*) FROM proposal_decisions").fetchone()[0] == (
                2 if first == "deferred" else 1
            )


@pytest.mark.parametrize(
    "actions", [("accept", "reject"), ("defer", "accept"), ("defer", "reject")]
)
def test_competing_browser_tabs_use_original_status_cas(database, actions):
    initialize(database)
    service, who, p = seed(database)
    r = service.prepare(who, p.id).revision
    app = create_app(replace(web_settings(), owner=who), service)
    barrier = Barrier(2)

    def run(action):
        with TestClient(app) as client:
            token = login(client)
            barrier.wait(timeout=10)
            return client.post(
                f"/reviews/{p.id}/{action}",
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
    with TestClient(create_app(replace(web_settings(), owner=who), service)) as client:
        token = login(client)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with psycopg.connect(database.database_url) as conn:
                conn.execute("SELECT id FROM proposals WHERE id=%s FOR UPDATE", (p.id,))
                future = pool.submit(
                    client.post,
                    f"/reviews/{p.id}/accept",
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
    with TestClient(create_app(replace(web_settings(), owner=other), service)) as client:
        token = login(client)
        assert client.get(f"/reviews/{p.id}").status_code == 404
        assert client.post(f"/reviews/{p.id}/refresh", data={"csrf": token}).status_code == 404
    assert service.get(who, p.id).revision is None
