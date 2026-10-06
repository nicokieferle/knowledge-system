"""Real loopback transports; synthetic search and ingress persistence services."""

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import warnings
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import httpx2
import pytest
import uvicorn

from examples.reference_client import client
from knowledge_system.mcp_server import create_mcp_server
from knowledge_system.proposal_ingress import SubmissionResult
from knowledge_system.proposal_ingress_web import create_app
from tests.test_mcp_server import FakeKnowledgeService, _free_port, _running_http_server
from tests.test_proposal_ingress import MCP_TOKEN, TOKEN, payload, settings

SCRIPT = Path(__file__).resolve().parents[1] / "examples/reference_client/client.py"
CLI_PYTHON = os.environ.get("TEST_REFERENCE_CLIENT_PYTHON", sys.executable)
PID = "65f9a888-494b-42a4-a27f-ac3fe8b8e8cf"


def run_cli(tmp_path, *args, read=MCP_TOKEN, submit=TOKEN):
    # No developer credentials, .env, proxies or product import path in child environment.
    env = {"PATH": os.environ["PATH"]}
    if read is not None:
        env[client.READ_TOKEN] = read
    if submit is not None:
        env[client.SUBMIT_TOKEN] = submit
    result = subprocess.run(
        [CLI_PYTHON, str(SCRIPT), *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        check=False,
        timeout=15,
    )
    for secret in (read, submit):
        if secret:
            assert secret.encode() not in result.stdout + result.stderr
    return result


@contextmanager
def ingress_server(service):
    port = _free_port()
    app = create_app(replace(settings(), allowed_hosts=(f"127.0.0.1:{port}",)), service)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            access_log=False,
            log_level="error",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() >= deadline:
                pytest.fail("local ingress failed to start")
            time.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()


def input_files(tmp_path):
    markdown = tmp_path / "draft.md"
    markdown.write_bytes(payload()["proposed_content"].encode())
    metadata = tmp_path / "metadata.json"
    metadata.write_text(
        json.dumps({k: v for k, v in payload().items() if k != "proposed_content"}),
        encoding="utf-8",
    )
    return markdown, metadata


def test_real_client_search_original_submit_replay_status(tmp_path):
    original = "# Synthetisches Original\r\n\r\nUnverändert α.\r\n"
    search = FakeKnowledgeService()
    search.get_document = Mock(
        return_value=Mock(
            source_id="knowledge-git",
            source_path="economics/inflation.md",
            content=original,
        )
    )
    service = Mock()
    receipt = {"proposal_id": PID, "conversation_id": str(uuid4()), "submission_state": "recorded"}
    service.submit.side_effect = [SubmissionResult(receipt, True), SubmissionResult(receipt, False)]
    service.status.return_value = {
        "proposal_id": PID,
        "proposal_status": "pending",
        "accepted_review_id": None,
        "apply_status": None,
        "index_status": None,
    }
    md, meta = input_files(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    server = create_mcp_server(search, allowed_source_ids=frozenset({"knowledge-git"}))
    with _running_http_server(server, token=MCP_TOKEN) as url, ingress_server(service) as ingress:
        result = run_cli(tmp_path, "search", "synthetische These", "--mcp-url", url + "/mcp")
        assert result.returncode == 0, result.stderr
        hit = json.loads(result.stdout)["results"][0]
        result = run_cli(
            tmp_path, "get", hit["source_id"], hit["source_path"], "--mcp-url", url + "/mcp"
        )
        assert result.returncode == 0 and result.stdout == original.encode()
        assert search.search_calls == [("synthetische These", "fast", 5)]
        key = str(uuid4())
        args = ("submit", str(md), str(meta), "--idempotency-key", key, "--ingress-url", ingress)
        result = run_cli(tmp_path, *args)
        assert result.returncode == 0 and PID.encode() in result.stdout
        result = run_cli(tmp_path, *args)
        assert result.returncode == 0 and b"Replay" in result.stdout
        calls = service.submit.call_args_list
        assert len(calls) == 2
        for call in calls:
            assert str(call.args[2]) == key
            assert call.args[3].proposed_content.encode() == md.read_bytes()
        result = run_cli(tmp_path, "status", PID, "--ingress-url", ingress)
        assert result.returncode == 0 and b"Wartet auf menschliches Review" in result.stdout
        assert result.stdout.count(b"Kein Apply-Journal") == 2
        service.status.assert_called_once_with(
            settings().client_id, settings().owner, client.UUID(PID)
        )
        # Neither credential grants the other boundary's authorization.
        assert (
            run_cli(
                tmp_path, "search", "test", "--mcp-url", url + "/mcp", read=TOKEN, submit=None
            ).returncode
            == 1
        )
        result = run_cli(
            tmp_path, "status", PID, "--ingress-url", ingress, read=None, submit=MCP_TOKEN
        )
        assert result.returncode == 1 and b"Authentifizierung" in result.stderr
    assert service.submit.call_count == 2 and service.status.call_count == 1
    assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before


@pytest.mark.parametrize(
    "proposal,apply,index,label",
    [
        ("pending", None, None, "Wartet"),
        ("deferred", None, None, "Zurückgestellt"),
        ("rejected", None, None, "Abgelehnt"),
        ("accepted", None, None, "Kein Apply-Journal"),
        ("accepted", "pending", "pending", "Ausstehend"),
        ("accepted", "conflict", "pending", "Konflikt"),
        ("accepted", "failed", "pending", "Fehlgeschlagen"),
        ("accepted", "applied", "failed", "Indexierung: Fehlgeschlagen"),
        ("accepted", "applied", "indexed", "Indexiert"),
    ],
)
def test_status_distinguishes_review_file_and_index(proposal, apply, index, label):
    body = {
        "proposal_id": PID,
        "proposal_status": proposal,
        "accepted_review_id": str(uuid4()) if proposal == "accepted" else None,
        "apply_status": apply,
        "index_status": index,
    }
    output = client.status_text(body, PID)
    assert label in output
    if apply == "applied":
        assert "Datei-Apply: Angewendet" in output


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8000/mcp",
        "http://localhost:8000/mcp",
        "http://remote:8000/mcp",
        "http://token@127.0.0.1:8000/mcp",
        "http://127.0.0.1/mcp",
        "http://127.0.0.1:8000/mcp?token=secret",
        "http://127.0.0.1:8000/mcp#fragment",
        "http://127.0.0.1:8000/mcp\n",
        "http://127.0.0.1:8000/%2fremote",
    ],
)
def test_rejects_urls_before_network(url):
    with pytest.raises(client.ClientError):
        client.local_url(url)


@pytest.mark.parametrize(
    "code,label",
    [
        (401, "Authentifizierung"),
        (404, "nicht verfügbar"),
        (409, "Konflikt"),
        (413, "groß"),
        (422, "ungültig"),
        (503, "vorübergehend"),
        (302, "Unerwartete"),
    ],
)
def test_private_error_bodies_and_redirects_are_not_exposed(monkeypatch, code, label):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer " + TOKEN
        return httpx2.Response(
            code, text="private " + TOKEN, headers={"Location": "http://remote.invalid/stolen"}
        )

    factory = httpx2.AsyncClient

    def http(**kwargs):
        assert kwargs["follow_redirects"] is False and kwargs["trust_env"] is False
        return factory(transport=httpx2.MockTransport(handler), **kwargs)

    monkeypatch.setattr(client.httpx2, "AsyncClient", http)
    with pytest.raises(client.ClientError, match=label) as error:
        asyncio.run(
            client.ingress_request("http://127.0.0.1:8081/v1/proposals/" + PID + "/status", TOKEN)
        )
    assert TOKEN not in str(error.value) and "private" not in str(error.value)
    assert len(requests) == 1


def test_cli_failures_never_echo_tokens_or_exception_details(tmp_path):
    for args in (
        ("status", TOKEN),
        ("search", "q", "--token", TOKEN),
        ("search", "q", "--mode", TOKEN),
    ):
        result = run_cli(tmp_path, *args)
        assert result.returncode != 0 and b"Traceback" not in result.stderr
    result = run_cli(tmp_path, "status", PID, read=None, submit=None)
    assert result.returncode == 1 and b"Token fehlt" in result.stderr
    assert run_cli(tmp_path, "status", PID, read=TOKEN, submit=TOKEN).returncode == 1
    result = run_cli(tmp_path, "search", "q", "--mcp-url", "http://127.0.0.1:1/mcp")
    assert result.returncode == 1 and b"Verbindung" in result.stderr


@pytest.mark.parametrize("kind", ["utf8", "limit", "duplicate", "unknown"])
def test_file_errors_do_not_submit(tmp_path, kind):
    md, meta = input_files(tmp_path)
    if kind == "utf8":
        md.write_bytes(b"\xff")
    elif kind == "limit":
        md.write_bytes(b"x" * 128_001)
    elif kind == "duplicate":
        meta.write_text('{"summary":"one","summary":"two"}')
    else:
        meta.write_text(json.dumps(payload()))  # proposed_content is not a metadata field
    with pytest.raises(client.ClientError):
        client.submission(md, meta)


def test_no_tokens_or_terminal_controls_in_output_or_submission(monkeypatch, tmp_path):
    # Quotes/backslashes must not bypass token detection through JSON escaping.
    token = 'synthetic-secret-with-quote"and-backslash\\1234'
    monkeypatch.setenv(client.SUBMIT_TOKEN, token)
    with pytest.raises(client.ClientError):
        client.protect_values({"results": [{"content": token}]}, MCP_TOKEN)
    with pytest.raises(client.ClientError):
        client.protect_values({"results": [{"content": "\x1b[31m"}]}, MCP_TOKEN)
    md, meta = input_files(tmp_path)
    md.write_text(token)
    args = client.parser().parse_args(
        ["submit", str(md), str(meta), "--idempotency-key", str(uuid4())]
    )
    with pytest.raises(client.ClientError):
        asyncio.run(client.execute(args, token))


def test_empty_results_and_tool_error(tmp_path):
    service = FakeKnowledgeService()
    service.search = Mock(return_value=[])
    with _running_http_server(create_mcp_server(service), token=MCP_TOKEN) as url:
        result = run_cli(tmp_path, "search", "missing", "--mcp-url", url + "/mcp")
        assert result.returncode == 0 and result.stdout == b"Keine Treffer.\n"
        result = run_cli(tmp_path, "search", " ", "--mcp-url", url + "/mcp")
        assert result.returncode == 1 and b"Traceback" not in result.stderr


def test_hidden_prompt_never_falls_back_to_echo(monkeypatch):
    monkeypatch.delenv(client.READ_TOKEN, raising=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def unsafe_prompt(prompt):
        warnings.warn("Cannot control echo", client.getpass.GetPassWarning)
        pytest.fail("must not proceed to echoed token input")

    monkeypatch.setattr(client.getpass, "getpass", unsafe_prompt)
    with pytest.raises(client.ClientError, match="Verdeckte"):
        client.credential(client.READ_TOKEN)


def test_status_requires_consistent_accepted_revision():
    body = {
        "proposal_id": PID,
        "proposal_status": "accepted",
        "accepted_review_id": None,
        "apply_status": None,
        "index_status": None,
    }
    with pytest.raises(client.ClientError, match="konsistente"):
        client.status_text(body, PID)
