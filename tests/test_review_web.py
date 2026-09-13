import re
import secrets
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Event
from unittest.mock import Mock
from uuid import uuid4

import pytest
from argon2 import PasswordHasher
from argon2.profiles import CHEAPEST
from fastapi.testclient import TestClient

from knowledge_system.client_state import ClientIdentity
from knowledge_system.proposal_review import (
    ProposalNotFound,
    ProposalReviewService,
    ReviewContext,
    ReviewForbidden,
    ReviewPage,
)
from knowledge_system.review_auth import ReviewWebSettings, Sessions, browser_base, internal_target
from knowledge_system.review_web import COOKIE, create_app
from knowledge_system.sources import GitMarkdownSource
from tests.test_proposal_review import MemoryReviewStore

PASSWORD = "synthetic local password only"


def web_settings():
    return ReviewWebSettings(
        PasswordHasher.from_parameters(CHEAPEST).hash(PASSWORD),
        secrets.token_urlsafe(32),
        ClientIdentity("test", "chat", "user"),
        mode="test",
        allowed_hosts=("testserver",),
    )


def csrf(response):
    return re.search(r'name="csrf" value="([^"]+)"', response.text)[1]


def login(client):
    page = client.get("/login")
    old = client.cookies.get(COOKIE)
    result = client.post(
        "/login", data={"csrf": csrf(page), "password": PASSWORD}, follow_redirects=False
    )
    assert result.status_code == 303
    assert client.cookies.get(COOKIE) != old
    return csrf(client.get("/reviews"))


@pytest.fixture
def web(tmp_path):
    store = MemoryReviewStore()
    store.queue = Mock(return_value=ReviewPage((), 0, 0))
    store.context = Mock(return_value=())
    service = ProposalReviewService(store, GitMarkdownSource(tmp_path))
    settings = web_settings()
    app = create_app(settings, service)
    with TestClient(app) as client:
        yield client, app, store, service, settings


def test_login_rotation_logout_and_protected_routes(web):
    client, app, store, _, _ = web
    assert client.get("/reviews", follow_redirects=False).status_code == 303
    page = client.get("/login")
    old_cookie = client.cookies.get(COOKIE)
    assert "HttpOnly" in page.headers["set-cookie"]
    assert "SameSite=strict" in page.headers["set-cookie"]
    assert client.post("/login", data={"csrf": csrf(page), "password": "wrong"}).status_code == 401
    token = login(client)
    assert (
        app.state.sessions.get(old_cookie) is None
        or not app.state.sessions.get(old_cookie).authenticated
    )
    cookie = client.cookies.get(COOKIE)
    result = client.post("/logout", data={"csrf": token}, follow_redirects=False)
    assert result.status_code == 303 and app.state.sessions.get(cookie) is None
    assert client.post(f"/reviews/{store.view.proposal.id}/accept", data={}).status_code == 401


@pytest.mark.parametrize("token", [None, "wrong", "other-session"])
@pytest.mark.parametrize("route", ["/login", "/logout", "/refresh", "/accept", "/reject", "/defer"])
def test_every_post_requires_session_bound_csrf(web, token, route):
    client, app, store, _, _ = web
    login(client)
    if token == "other-session":
        token = app.state.sessions.create().csrf
    path = route if route in ("/login", "/logout") else f"/reviews/{store.view.proposal.id}{route}"
    response = client.post(path, data={"csrf": token} if token else {"password": "x"})
    assert response.status_code == 403
    assert not store.decisions and not store.revisions


def test_detail_get_never_prepares_and_refresh_is_explicit(web):
    client, _, store, _, _ = web
    token = login(client)
    path = f"/reviews/{store.view.proposal.id}"
    assert client.get(path).status_code == 200 and not store.revisions
    assert client.get(path + "/refresh").status_code == 405
    assert (
        client.post(path + "/refresh", data={"csrf": token}, follow_redirects=False).status_code
        == 303
    )
    assert len(store.revisions) == 1
    page = client.get(path)
    assert "Vollständiger Diff" in page.text and str(store.view.revision.id) in page.text


def test_refresh_preserves_exact_bytes_unless_edit_is_explicit_and_rejects_old_tab(web):
    client, _, store, service, settings = web
    token = login(client)
    pid = store.view.proposal.id
    first = service.prepare(settings.owner, pid, new_content="LF only\n").revision
    data = {"csrf": token, "review_id": str(first.id), "content": "LF only\r\n"}
    assert (
        client.post(f"/reviews/{pid}/refresh", data=data, follow_redirects=False).status_code == 303
    )
    assert store.view.revision == first
    data["edit_content"] = "yes"
    assert (
        client.post(f"/reviews/{pid}/refresh", data=data, follow_redirects=False).status_code == 303
    )
    assert store.view.revision.content.new_content == "LF only\r\n"
    assert client.post(f"/reviews/{pid}/refresh", data=data).status_code == 409


@pytest.mark.parametrize(
    "action,status", [("accept", "accepted"), ("reject", "rejected"), ("defer", "deferred")]
)
def test_browser_decision_passes_exact_revision_and_observed_status(web, action, status):
    client, _, store, service, settings = web
    token = login(client)
    view = service.prepare(settings.owner, store.view.proposal.id)
    original = store.decide
    store.decide = Mock(side_effect=original)
    path = f"/reviews/{view.proposal.id}/{action}"
    data = {"csrf": token, "review_id": str(view.revision.id), "status": "pending"}
    if action != "defer":
        assert client.post(path, data=data).status_code == 200
        store.decide.assert_not_called()
        data["confirmed"] = "yes"
    response = client.post(path, data=data, follow_redirects=False)
    assert response.status_code == 303
    assert store.decide.call_args.args[2:] == (view.revision.id, status, "pending")
    assert client.get(response.headers["location"]).status_code == 200
    assert len(store.decisions) == 1


def test_stale_and_superseded_accept_are_rejected(web, tmp_path):
    client, _, store, service, settings = web
    token = login(client)
    first = service.prepare(settings.owner, store.view.proposal.id).revision
    second = service.prepare(settings.owner, store.view.proposal.id, new_content="second").revision
    path = f"/reviews/{store.view.proposal.id}/accept"
    data = {"csrf": token, "review_id": str(first.id), "status": "pending", "confirmed": "yes"}
    assert client.post(path, data=data).status_code == 409
    (tmp_path / "notes.md").write_text("changed")
    data["review_id"] = str(second.id)
    assert client.post(path, data=data).status_code == 409
    assert not store.decisions


def test_owner_is_server_side_and_foreign_matches_missing(web):
    client, _, store, _, settings = web
    login(client)
    original = store.get
    store.get = Mock(side_effect=original)
    path = f"/reviews/{store.view.proposal.id}?owner=attacker"
    client.get(path)
    assert store.get.call_args.args[0] == settings.owner
    store.get.side_effect = ReviewForbidden()
    foreign = client.get(path)
    store.get.side_effect = ProposalNotFound()
    missing = client.get(path)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.text == missing.text


def test_escaped_context_diff_path_large_unicode_and_safe_errors(web, caplog):
    client, _, store, service, settings = web
    login(client)
    payload = '<script>alert("PRIVATE")</script>'
    store.context.return_value = (ReviewContext(9, payload + "🧪" * 20000),)
    view = service.prepare(
        settings.owner, store.view.proposal.id, new_content=payload + "a" * 120000
    )
    store.view = replace(
        view, proposal=replace(view.proposal, target_source_path=payload, summary=payload)
    )
    result = client.get(f"/reviews/{view.proposal.id}")
    assert result.status_code == 200
    assert payload not in result.text and "&lt;script&gt;" in result.text
    assert "a" * 120000 in result.text and "🧪" * 20000 in result.text
    store.get = Mock(side_effect=RuntimeError(payload))
    failed = client.get(f"/reviews/{uuid4()}")
    assert failed.status_code == 500 and "Referenz:" in failed.text
    assert "PRIVATE" not in caplog.text and payload not in failed.text


def test_filter_pagination_and_security_headers(web):
    client, _, store, _, settings = web
    login(client)
    response = client.get("/reviews?status=accepted&page=5")
    assert response.status_code == 200
    store.queue.assert_called_with(settings.owner, ("accepted",), 120, 30)
    for url in ("/reviews?page=0", "/reviews?page=no", "/reviews?status=bad", "/reviews/not-uuid"):
        assert client.get(url).status_code == 400
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert client.get("/login", headers={"Host": "attacker.invalid"}).status_code == 400


@pytest.mark.parametrize(
    "value",
    ["https://evil.invalid", "//evil.invalid", "/\\evil", "/reviews?next=evil", "/%2f%2fevil"],
)
def test_internal_redirect_allowlist(value):
    assert internal_target(value) == "/reviews"


def test_session_expiration_throttle_and_production_fail_closed(monkeypatch):
    settings = web_settings()
    clock = [1000.0]
    sessions = Sessions(settings, clock=lambda: clock[0])
    session = sessions.create()
    for _ in range(5):
        assert sessions.login(session, "wrong") is None
    with pytest.raises(OverflowError):
        sessions.login(session, PASSWORD)
    clock[0] += 61
    logged = sessions.login(session, PASSWORD)
    assert logged.authenticated and sessions.get(sessions.cookie(session)) is None
    clock[0] += settings.session_seconds
    assert sessions.get(sessions.cookie(logged)) is None
    monkeypatch.delenv("REVIEW_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("REVIEW_SESSION_SECRET", raising=False)
    with pytest.raises(ValueError):
        ReviewWebSettings.from_environment()
    strong = replace(settings, password_hash=PasswordHasher().hash(PASSWORD), mode="production")
    app = create_app(strong, Mock())
    with TestClient(app, base_url="https://testserver") as client:
        assert "Secure" in client.get("/login").headers["set-cookie"]


@pytest.mark.parametrize(
    "value",
    [
        "javascript:alert(1)",
        "https://user:password@host",
        "https://host/?token=secret",
        "http://private.invalid",
    ],
)
def test_configured_browser_link_rejects_unsafe_urls(value):
    with pytest.raises(ValueError):
        browser_base(value)


def test_cookie_tampering_logout_get_and_oversized_duplicate_forms(web):
    client, app, store, _, _ = web
    token = login(client)
    cookie = client.cookies.get(COOKIE)
    assert app.state.sessions.get(cookie + "tampered") is None
    assert client.get("/logout").status_code == 405
    assert app.state.sessions.get(cookie).authenticated
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    assert (
        client.post("/logout", content=f"csrf={token}&csrf={token}", headers=headers).status_code
        == 403
    )
    assert (
        client.post("/logout", content="csrf=" + "x" * 800001, headers=headers).status_code == 413
    )
    assert not store.decisions


def test_login_return_path_is_safe_and_csrf_rotates(web):
    client, app, store, _, _ = web
    path = f"/reviews/{store.view.proposal.id}"
    page = client.get("/login?next=" + path)
    old_csrf = csrf(page)
    response = client.post(
        "/login",
        data={"csrf": old_csrf, "password": PASSWORD, "next": path},
        follow_redirects=False,
    )
    assert response.headers["location"] == path
    assert app.state.sessions.get(client.cookies.get(COOKIE)).csrf != old_csrf
    assert client.post("/logout", data={"csrf": old_csrf}).status_code == 403


def test_legacy_unprepared_path_is_escaped(web):
    client, _, store, _, _ = web
    login(client)
    payload = '"><img src=x onerror=alert(1)>'
    store.view = replace(
        store.view, proposal=replace(store.view.proposal, target_source_path=payload)
    )
    response = client.get(f"/reviews/{store.view.proposal.id}")
    assert response.status_code == 200
    assert payload not in response.text and "&lt;img" in response.text


def test_full_anonymous_session_store_evicts_oldest_for_new_login_session():
    sessions = Sessions(web_settings())
    oldest = sessions.create()
    for _ in range(255):
        sessions.create()

    replacement = sessions.create()

    assert len(sessions.entries) == 256
    assert oldest.id not in sessions.entries
    assert replacement.id in sessions.entries


def test_anonymous_session_eviction_preserves_authenticated_session():
    sessions = Sessions(web_settings())
    authenticated = sessions.create(authenticated=True)
    oldest_anonymous = sessions.create()
    survivors = [sessions.create() for _ in range(254)]

    replacement = sessions.create()

    assert len(sessions.entries) == 256
    assert sessions.entries[authenticated.id] == authenticated
    assert oldest_anonymous.id not in sessions.entries
    assert survivors[0].id in sessions.entries
    assert replacement.id in sessions.entries


def test_full_authenticated_session_store_fails_closed_without_mutation():
    sessions = Sessions(web_settings())
    for _ in range(256):
        sessions.create(authenticated=True)
    before = list(sessions.entries.items())

    with pytest.raises(OverflowError):
        sessions.create()

    assert list(sessions.entries.items()) == before
    assert all(session.authenticated for session in sessions.entries.values())


def test_expired_session_is_cleaned_at_capacity_before_anonymous_eviction():
    clock = [1000.0]
    sessions = Sessions(replace(web_settings(), session_seconds=60), clock=lambda: clock[0])
    expired_authenticated = sessions.create(authenticated=True)
    oldest_live_anonymous = sessions.create()
    for _ in range(254):
        sessions.create()
    assert len(sessions.entries) == 256

    clock[0] += 61
    replacement = sessions.create()

    assert len(sessions.entries) == 256
    assert expired_authenticated.id not in sessions.entries
    assert oldest_live_anonymous.id in sessions.entries
    assert replacement.id in sessions.entries


class _TrackingEntries(dict):
    def __init__(self, entries):
        super().__init__(entries)
        self.max_size = len(self)

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.max_size = max(self.max_size, len(self))


def test_parallel_anonymous_eviction_stays_bounded_and_preserves_authenticated_sessions():
    sessions = Sessions(web_settings())
    authenticated = [sessions.create(authenticated=True) for _ in range(8)]
    for _ in range(242):
        sessions.create()
    sessions.entries = _TrackingEntries(sessions.entries)
    worker_count = 16
    start = Barrier(worker_count + 1)

    def create_after_barrier():
        start.wait(timeout=5)
        return sessions.create()

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(create_after_barrier) for _ in range(worker_count)]
        start.wait(timeout=5)
        created = [future.result(timeout=5) for future in futures]

    assert len(sessions.entries) == 256
    assert sessions.entries.max_size <= 256
    assert all(session.id in sessions.entries for session in authenticated)
    assert len({session.id for session in created}) == len(created)
    assert all(session.id in sessions.entries for session in created)
    assert all(session.id == sid for sid, session in sessions.entries.items())


def test_concurrent_login_rotation_and_eviction_preserve_rotated_session():
    sessions = Sessions(web_settings())
    authenticated = [sessions.create(authenticated=True) for _ in range(4)]
    login_session = sessions.create()
    for _ in range(251):
        sessions.create()
    sessions.entries = _TrackingEntries(sessions.entries)
    verify_started = Event()
    allow_verify = Event()
    worker_count = 8
    insert_start = Barrier(worker_count + 1)

    class BlockingHasher:
        def verify(self, password_hash, password):
            assert password_hash == sessions.settings.password_hash
            assert password == PASSWORD
            verify_started.set()
            assert allow_verify.wait(timeout=5)

    sessions.hasher = BlockingHasher()

    def create_after_barrier():
        insert_start.wait(timeout=5)
        return sessions.create()

    with ThreadPoolExecutor(max_workers=worker_count + 1) as executor:
        login_future = executor.submit(sessions.login, login_session, PASSWORD)
        assert verify_started.wait(timeout=5)
        create_futures = [executor.submit(create_after_barrier) for _ in range(worker_count)]
        insert_start.wait(timeout=5)
        allow_verify.set()
        rotated = login_future.result(timeout=5)
        created = [future.result(timeout=5) for future in create_futures]

    assert rotated is not None and rotated.authenticated
    assert len(sessions.entries) == 256
    assert sessions.entries.max_size <= 256
    assert login_session.id not in sessions.entries
    assert sessions.entries[rotated.id] == rotated
    assert sum(session.id == rotated.id for session in sessions.entries.values()) == 1
    assert all(session.id in sessions.entries for session in authenticated)
    assert len({session.id for session in created}) == len(created)
    assert all(session.id == sid for sid, session in sessions.entries.items())
