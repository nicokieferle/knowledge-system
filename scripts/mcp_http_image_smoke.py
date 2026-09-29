"""Exercise the built image's real MCP HTTP entry point against isolated synthetic data."""

from __future__ import annotations

import asyncio
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

import httpx2
import psycopg
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from psycopg import sql


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_server(process: subprocess.Popen[bytes], port: int) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("MCP HTTP process stopped during startup")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("MCP HTTP process did not start")


async def _exercise(port: int, token: str) -> None:
    url = f"http://127.0.0.1:{port}/mcp"
    async with httpx2.AsyncClient() as unauthorized:
        response = await unauthorized.post(
            url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        assert response.status_code == 401
        assert response.text == "Unauthorized"
        response = await unauthorized.post(
            url,
            headers={"Authorization": "Bearer wrong"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert response.status_code == 401
        assert "Original synthetic document" not in response.text

    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http_client,
        Client(streamable_http_client(url, http_client=http_client), mode="legacy") as client,
    ):
        listed = await client.list_tools()
        assert [tool.name for tool in listed.tools] == ["search_knowledge", "get_document"]
        result = await client.call_tool(
            "search_knowledge", {"query": "synthetic", "mode": "fast", "limit": 1}
        )
        assert result.is_error is False
        assert result.structured_content["results"][0]["source_id"] == "knowledge-git"
        assert result.structured_content["results"][0]["source_path"] == "note.md"
        document = await client.call_tool(
            "get_document", {"source_id": "knowledge-git", "source_path": "note.md"}
        )
        assert document.structured_content == {
            "source_id": "knowledge-git",
            "source_path": "note.md",
            "content": "# Synthetic\n\nOriginal synthetic document.",
        }
        unavailable = await client.call_tool("apply_accepted_revision", {})
        assert unavailable.is_error is True


def main() -> None:
    address = os.environ["TEST_DATABASE_URL"]
    parsed = urlsplit(address)
    assert parsed.hostname == "postgres" and parsed.path == "/knowledge_ci_smoke"
    assert not parsed.query
    schema = "mcp_http_" + uuid4().hex
    with psycopg.connect(address, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    try:
        scoped_address = address + "?" + urlencode({"options": f"-csearch_path={schema},public"})
        with psycopg.connect(scoped_address, autocommit=True) as conn:
            conn.execute(
                "CREATE TABLE chunks (chunk_key text, source_id text, source_path text, "
                "heading_path text, content text, ordinal integer)"
            )
            conn.execute(
                "INSERT INTO chunks VALUES "
                "('synthetic', 'knowledge-git', 'note.md', 'Synthetic', "
                "'Synthetic indexed passage.', 1)"
            )

        with tempfile.TemporaryDirectory(prefix="mcp-http-smoke-") as directory:
            root = Path(directory)
            (root / "note.md").write_text(
                "# Synthetic\n\nOriginal synthetic document.", encoding="utf-8"
            )
            token = secrets.token_hex(32)
            port = _free_port()
            env = os.environ.copy()
            env.update(
                {
                    "DATABASE_URL": scoped_address,
                    "KNOWLEDGE_ROOT": str(root),
                    "MCP_TRANSPORT": "streamable-http",
                    "MCP_HOST": "127.0.0.1",
                    "MCP_PORT": str(port),
                    "MCP_HTTP_CLIENT_ID": "image-smoke",
                    "MCP_HTTP_BEARER_TOKEN": token,
                }
            )
            process = subprocess.Popen(
                [sys.executable, "-m", "knowledge_system.mcp_server"],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                _wait_for_server(process, port)
                asyncio.run(_exercise(port, token))
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
    finally:
        with psycopg.connect(address, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
    print("mcp_http_image_smoke=passed; valid_and_invalid_auth=true; synthetic_data=true")


if __name__ == "__main__":
    main()
