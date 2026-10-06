import asyncio
import json
from dataclasses import replace
from unittest.mock import Mock
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from knowledge_system.client_state import ClientIdentity
from knowledge_system.proposal_ingress import (
    IngressError,
    SubmissionResult,
    parse_submission,
)
from knowledge_system.proposal_ingress_config import IngressSettings
from knowledge_system.proposal_ingress_web import create_app

TOKEN = "synthetic-submit-credential-1234567890"
MCP_TOKEN = "synthetic-read-credential-123456789012"
OWNER = ClientIdentity("test", "chat", "user")


def settings():
    return IngressSettings("external-client", TOKEN, OWNER, ("testserver",))


def payload():
    return {
        "summary": "Synthetischer Vorschlag",
        "reason": "Suche: keine verwandte These gefunden.",
        "proposed_content": "# Hypothese\r\n\r\nÄnderung α.\r\n",
        "target_source_path": "notes/example.md",
        "provenance": {
            "source_ref": "opaque:<script>alert(1)</script>",
            "origin_kind": "model_output",
            "statement_type": "hypothesis",
            "original_text": "Ursprünglich ä α\r\n<script>alert(1)</script>",
        },
    }


def headers(key=None):
    return {"Authorization": "Bearer " + TOKEN, "Idempotency-Key": str(key or uuid4())}


def test_http_valid_draft_and_receipt():
    service = Mock()
    receipt = {
        "proposal_id": str(uuid4()),
        "conversation_id": str(uuid4()),
        "submission_state": "recorded",
    }
    service.submit.return_value = SubmissionResult(receipt, True)
    with TestClient(create_app(settings(), service)) as client:
        response = client.post("/v1/proposals", json=payload(), headers=headers())
        assert response.status_code == 201 and response.json() == receipt
        submitted = service.submit.call_args.args
        assert submitted[:2] == (settings().client_id, OWNER)
        assert submitted[3].proposed_content == payload()["proposed_content"]
        service.submit.return_value = SubmissionResult(receipt, False)
        assert client.post("/v1/proposals", json=payload(), headers=headers()).status_code == 200


def test_http_status_boundary_and_safe_failures():
    service = Mock()
    pid = uuid4()
    result = {
        "proposal_id": str(pid),
        "proposal_status": "pending",
        "accepted_review_id": None,
        "apply_status": None,
        "index_status": None,
    }
    service.status.return_value = result
    path = f"/v1/proposals/{pid}/status"
    with TestClient(create_app(settings(), service)) as client:
        r = client.get(path, headers=headers())
        assert r.status_code == 200 and r.json() == result
        assert r.headers["cache-control"] == "no-store"
        service.status.assert_called_once_with(settings().client_id, OWNER, pid)
        service.status.reset_mock()
        for invalid in ("invalid", "{" + str(pid) + "}", str(pid).upper()):
            r = client.get(f"/v1/proposals/{invalid}/status", headers=headers())
            assert r.status_code == 404 and r.json()["error"] == "not_found"
        assert client.get(path + "?owner=other", headers=headers()).status_code == 400
        assert client.get(path, headers=headers() | {"Host": "foreign"}).status_code == 400
        assert client.post(path, headers=headers()).status_code == 405
        service.status.assert_not_called()
        service.status.side_effect = IngressError(404, "not_found")
        assert client.get(path, headers=headers()).json()["error"] == "not_found"
        service.status.side_effect = psycopg.OperationalError("private SQL credential")
        r = client.get(path, headers=headers())
        assert r.status_code == 503 and r.json()["error"] == "status_unavailable"
        assert "private" not in r.text
        service.submit.assert_not_called()


@pytest.mark.parametrize(
    "credential",
    [
        [],
        [("Authorization", "Bearer wrong")],
        [("Authorization", "Bearer " + MCP_TOKEN)],
        [("Authorization", "Bearer " + TOKEN)] * 2,
        [("Authorization", "Bearer " + TOKEN), ("X-Api-Key", TOKEN)],
        [("Cookie", "session=" + TOKEN)],
    ],
)
def test_auth_precedes_body_and_no_store_access(credential):
    service = Mock()
    with TestClient(create_app(settings(), service)) as client:
        response = client.post("/v1/proposals", content=b"{bad", headers=credential)
        denied_status = client.get(f"/v1/proposals/{uuid4()}/status", headers=credential)
    assert response.status_code == 401 and response.headers["www-authenticate"] == "Bearer"
    assert denied_status.status_code == 401
    assert denied_status.headers["www-authenticate"] == "Bearer"
    assert set(response.json()) == {"error", "correlation_id"}
    service.submit.assert_not_called()
    service.status.assert_not_called()


def test_auth_does_not_receive_body():
    app = create_app(settings(), Mock())

    async def receive():
        pytest.fail("unauthenticated body read")

    sent = []

    async def send(message):
        sent.append(message)

    asyncio.run(app({"type": "http", "headers": []}, receive, send))
    assert sent[0]["status"] == 401


@pytest.mark.parametrize(
    "field",
    [
        "owner",
        "status",
        "conversation_id",
        "trigger_message_id",
        "review_id",
        "apply_id",
        "base_revision",
        "target_source_id",
    ],
)
def test_internal_fields_are_rejected(field):
    body = payload() | {field: "forbidden"}
    service = Mock()
    with TestClient(create_app(settings(), service)) as client:
        r = client.post("/v1/proposals", json=body, headers=headers())
    assert r.status_code == 422
    service.submit.assert_not_called()


@pytest.mark.parametrize("path", ["../x.md", "/x.md", "überblick.md", "README.md", "CON.md"])
def test_unsafe_write_target(path):
    with pytest.raises(IngressError) as error:
        parse_submission(json.dumps(payload() | {"target_source_path": path}).encode())
    assert error.value.status == 422


@pytest.mark.parametrize(
    "body,status",
    [
        (b'{"summary":"a","summary":"b"}', 400),
        (b'{"provenance":{"source_ref":"a","source_ref":"b"}}', 400),
        (b"{", 400),
        (b"NaN", 400),
        (b"[]", 422),
        (json.dumps(payload() | {"summary": ""}).encode(), 422),
        (json.dumps(payload() | {"summary": "ä" * 2001}).encode(), 413),
        (json.dumps(payload() | {"proposed_content": "x" * 128001}).encode(), 413),
        (json.dumps(payload() | {"proposed_content": "a\x1bb"}).encode(), 422),
        (json.dumps(payload() | {"provenance": {}}).encode(), 422),
        (json.dumps(payload() | {"title": "\ud800"}).encode(), 422),
    ],
)
def test_invalid_json_and_schema(body, status):
    service = Mock()
    with TestClient(create_app(settings(), service)) as client:
        r = client.post(
            "/v1/proposals", content=body, headers=headers() | {"Content-Type": "application/json"}
        )
    assert r.status_code == status
    service.submit.assert_not_called()


def test_http_headers_limits_routes_and_safe_errors():
    service = Mock()
    with TestClient(create_app(settings(), service)) as client:
        h = headers()
        assert (
            client.post(
                "/v1/proposals", json=payload(), headers={"Authorization": h["Authorization"]}
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/v1/proposals",
                json=payload(),
                headers=list(h.items()) + [("Idempotency-Key", str(uuid4()))],
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/v1/proposals",
                json=payload(),
                headers=h | {"Idempotency-Key": "{" + h["Idempotency-Key"] + "}"},
            ).status_code
            == 400
        )
        assert client.post("/v1/proposals", content="{}", headers=h).status_code == 415
        assert client.post("/v1/proposals?token=x", json=payload(), headers=h).status_code == 400
        assert (
            client.post(
                "/v1/proposals", json=payload(), headers=h | {"Host": "foreign.example"}
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/v1/proposals",
                content=b" " * 512001,
                headers=h | {"Content-Type": "application/json"},
            ).status_code
            == 413
        )
        assert client.get("/v1/proposals", headers=h).status_code == 405
        for path in ("/reviews", "/apply", "/docs", "/openapi.json", "/v1/proposals/"):
            assert client.post(path, headers=h).status_code == 404
        service.submit.assert_not_called()
        service.submit.side_effect = psycopg.OperationalError("secret SQL or credentials")
        r = client.post("/v1/proposals", json=payload(), headers=h)
        assert r.status_code == 503 and "secret" not in r.text
        service.submit.side_effect = IngressError(409, "binding_conflict")
        assert client.post("/v1/proposals", json=payload(), headers=h).status_code == 409


def test_fingerprint_optional_fields_order_and_exact_strings():
    p = payload()
    first = parse_submission(json.dumps(p).encode())
    second = parse_submission(
        json.dumps(dict(reversed(list(p.items()))) | {"title": None}).encode()
    )
    assert first.fingerprint == second.fingerprint
    for field in ("summary", "reason", "proposed_content", "title", "target_source_path"):
        changed = p | {field: "other.md" if field == "target_source_path" else "different"}
        assert parse_submission(json.dumps(changed).encode()).fingerprint != first.fingerprint
    for field in p["provenance"]:
        replacements = {"origin_kind": "human_statement", "statement_type": "fact"}
        changed = p | {
            "provenance": p["provenance"] | {field: replacements.get(field, "different")}
        }
        assert parse_submission(json.dumps(changed).encode()).fingerprint != first.fingerprint
    assert first.provenance.original_text == p["provenance"]["original_text"]


@pytest.mark.parametrize(
    "field,limit",
    [
        ("summary", 4000),
        ("reason", 4000),
        ("proposed_content", 128000),
        ("title", 256),
        ("original_text", 128000),
        ("source_ref", 1024),
    ],
)
def test_exact_utf8_limits(field, limit):
    p = payload()
    target = p["provenance"] if field in p["provenance"] else p
    target[field] = "ä" * (limit // 2)
    parse_submission(json.dumps(p).encode())
    target[field] += "x"
    with pytest.raises(IngressError) as exc:
        parse_submission(json.dumps(p).encode())
    assert exc.value.status == 413


def test_start_configuration_delegation_and_separate_secrets(monkeypatch):
    for prefix in ("PROPOSAL_INGRESS_OWNER_", "REVIEW_OWNER_"):
        for key, val in zip(("CLIENT_TYPE", "CHAT_ID", "USER_ID"), ("test", "chat", "user")):
            monkeypatch.setenv(prefix + key, val)
    monkeypatch.setenv("PROPOSAL_INGRESS_CLIENT_ID", "synthetic-client")
    monkeypatch.setenv("PROPOSAL_INGRESS_BEARER_TOKEN", TOKEN)
    assert IngressSettings.from_environment().owner == OWNER
    for key in ("MCP_HTTP_BEARER_TOKEN", "REVIEW_SESSION_SECRET", "REVIEW_PASSWORD_HASH"):
        monkeypatch.setenv(key, TOKEN)
        with pytest.raises(ValueError):
            IngressSettings.from_environment()
        monkeypatch.delenv(key)
    monkeypatch.setenv("REVIEW_OWNER_USER_ID", "foreign")
    with pytest.raises(ValueError):
        IngressSettings.from_environment()
    for change in (
        {"client_id": "x" * 65},
        {"client_id": "ä"},
        {"token": "short"},
        {"token": "a" * 32 + " "},
        {"owner": ClientIdentity("", "", "")},
        {"allowed_hosts": ("*",)},
        {"port": 0},
    ):
        with pytest.raises(ValueError):
            replace(settings(), **change)


@pytest.mark.parametrize(
    "change",
    [
        {"original_text": None},
        {"original_text": ""},
        {"origin_kind": "trusted"},
        {"statement_type": "verified_fact"},
        {"owner": "foreign"},
    ],
)
def test_provenance_is_required_typed_and_untrusted(change):
    p = payload()
    p["provenance"].update(change)
    with pytest.raises(IngressError) as error:
        parse_submission(json.dumps(p).encode())
    assert error.value.status == 422


def test_optional_target_and_title_null_and_invalid_required_fields():
    p = payload()
    del p["target_source_path"]
    parsed = parse_submission(json.dumps(p).encode())
    assert parsed.target_source_path is None and parsed.title is None
    assert (
        parse_submission(
            json.dumps(p | {"title": None, "target_source_path": None}).encode()
        ).fingerprint
        == parsed.fingerprint
    )
    for field in ("summary", "reason", "proposed_content", "provenance"):
        missing = dict(p)
        del missing[field]
        with pytest.raises(IngressError) as error:
            parse_submission(json.dumps(missing).encode())
        assert error.value.status == 422
