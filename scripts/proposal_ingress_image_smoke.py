"""Real ingress and browser processes in the built image with isolated PostgreSQL."""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import subprocess
import tempfile
import time
from html import unescape
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

import httpx2
import psycopg
from argon2 import PasswordHasher
from psycopg import sql

from knowledge_system.config import Settings
from knowledge_system.db import init_db


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start(command, env, port):
    process = subprocess.Popen(
        [command], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("Image process stopped during startup")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return process
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("Image process did not start")
    except BaseException:
        stop(process)
        raise


def stop(process):
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def exercise(env, address, root):
    token = secrets.token_hex(32)
    port, review_port = free_port(), free_port()
    password = secrets.token_hex(32)
    env.update(
        {
            "DATABASE_URL": address,
            "KNOWLEDGE_ROOT": str(root),
            "PROPOSAL_INGRESS_CLIENT_ID": "image-client",
            "PROPOSAL_INGRESS_BEARER_TOKEN": token,
            "PROPOSAL_INGRESS_HOST": "127.0.0.1",
            "PROPOSAL_INGRESS_PORT": str(port),
            "PROPOSAL_INGRESS_ALLOWED_HOSTS": f"127.0.0.1:{port}",
            "REVIEW_MODE": "development",
            "REVIEW_PASSWORD_HASH": PasswordHasher().hash(password),
            "REVIEW_SESSION_SECRET": secrets.token_hex(32),
            "REVIEW_LISTEN_HOST": "127.0.0.1",
            "REVIEW_PORT": str(review_port),
            "REVIEW_ALLOWED_HOSTS": f"127.0.0.1:{review_port}",
        }
    )
    for prefix in ("PROPOSAL_INGRESS_OWNER_", "REVIEW_OWNER_"):
        env.update(
            {
                prefix + "CLIENT_TYPE": "synthetic",
                prefix + "CHAT_ID": "chat",
                prefix + "USER_ID": "user",
            }
        )
    ingress = start("knowledge-proposal-ingress", env, port)
    try:
        browser = start("knowledge-review", env, review_port)
        try:
            with httpx2.Client(base_url=f"http://127.0.0.1:{port}") as client:
                body = {
                    "summary": "Synthetic image draft",
                    "reason": "Related search found no match.",
                    "proposed_content": "# Hypothesis\r\n\r\nSynthetic α.\r\n",
                    "target_source_path": "example.md",
                    "provenance": {
                        "source_ref": "opaque:synthetic/7\nMaschinen-Client: forged\r\nQuellenart: forged\u202e",
                        "origin_kind": "model_output",
                        "statement_type": "hypothesis",
                        "original_text": "Original ä α\r\n",
                    },
                }
                assert client.post("/v1/proposals", json=body).status_code == 401
                assert (
                    client.post(
                        "/v1/proposals", json=body, headers={"Authorization": "Bearer wrong"}
                    ).status_code
                    == 401
                )
                headers = {"Authorization": "Bearer " + token, "Idempotency-Key": str(uuid4())}
                first = client.post("/v1/proposals", json=body, headers=headers)
                assert first.status_code == 201
                receipt = first.json()
                replay = client.post("/v1/proposals", json=body, headers=headers)
                assert replay.status_code == 200 and replay.json() == receipt
                conflict = client.post(
                    "/v1/proposals", json=body | {"reason": "Different"}, headers=headers
                )
                assert conflict.status_code == 409
            with httpx2.Client(base_url=f"http://127.0.0.1:{review_port}") as client:
                pid = receipt["proposal_id"]
                assert (
                    client.post(
                        f"/reviews/{pid}/refresh", headers={"Authorization": "Bearer " + token}
                    ).status_code
                    == 401
                )
                page = client.get("/login")
                csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
                assert (
                    client.post("/login", data={"csrf": csrf, "password": password}).status_code
                    == 303
                )
                queue = client.get("/reviews")
                assert pid in queue.text and "Original ä α" in queue.text
                assert "Eingereichte Herkunftsangaben" not in queue.text
                page = client.get(f"/reviews/{pid}")
                assert page.status_code == 200
                assert "Original ä α" in page.text and "model_output" in page.text
                assert "hypothesis" in page.text and "image-client" in page.text
                contexts = re.findall(
                    r'<blockquote><p class="preserve">(.*?)</p>', page.text, re.DOTALL
                )
                provenance = unescape(contexts[1])
                reference_line = provenance.splitlines()[3]
                assert (
                    json.loads(reference_line.removeprefix("Quellenreferenz (JSON-String): "))
                    == body["provenance"]["source_ref"]
                )
                assert "\nMaschinen-Client: forged" not in provenance
                assert "\r\nQuellenart: forged" not in provenance
                assert "\u202e" not in provenance
            with psycopg.connect(address) as conn:
                assert conn.execute("SELECT count(*) FROM conversations").fetchone()[0] == 1
                assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 3
                assert (
                    conn.execute("SELECT proposed_content FROM proposals").fetchone()[0]
                    == body["proposed_content"]
                )
                for table in (
                    "proposal_reviews",
                    "proposal_decisions",
                    "proposal_applies",
                    "chunks",
                ):
                    assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
            assert not list(root.iterdir())
        finally:
            stop(browser)
    finally:
        stop(ingress)


def main():
    address = os.environ["TEST_DATABASE_URL"]
    parsed = urlsplit(address)
    assert (
        parsed.hostname == "postgres" and parsed.path == "/knowledge_ci_smoke" and not parsed.query
    )
    schema = "proposal_ingress_" + uuid4().hex
    with psycopg.connect(address, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        scoped = address + "?" + urlencode({"options": f"-csearch_path={schema},public"})
        with tempfile.TemporaryDirectory(prefix="proposal-ingress-smoke-") as directory:
            root = Path(directory)
            init_db(Settings(scoped, root, "unused", 384))
            exercise(os.environ.copy(), scoped, root)
    finally:
        with psycopg.connect(address, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
    print(
        "proposal_ingress_image_smoke=passed; real_http_browser_postgres=true; synthetic_data=true"
    )


if __name__ == "__main__":
    main()
