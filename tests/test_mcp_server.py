from __future__ import annotations

import asyncio
import os
import socket
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx2
import pytest
import uvicorn
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import TextContent

from knowledge_system.config import (
    MCPServerSettings,
    Settings,
    get_mcp_server_settings,
)
from knowledge_system.mcp_server import (
    create_http_app,
    create_loopback_transport_security,
    create_mcp_server,
    create_transport_security,
    run_mcp_server,
)
from knowledge_system.search import SearchResult
from knowledge_system.service import (
    KnowledgeIndexUnavailableError,
    KnowledgeSearchResult,
    KnowledgeService,
    SourceDocumentNotFoundError,
    UnknownSourceError,
)
from knowledge_system.sources import GitMarkdownSource, SourceDocument


class FakeKnowledgeService:
    def __init__(self) -> None:
        self.search_calls: list[tuple[str, str, int]] = []
        self.document_calls: list[tuple[str, str]] = []

    def search(
        self, query: str, mode: str, limit: int, source_ids: frozenset[str] | None = None
    ) -> list[KnowledgeSearchResult]:
        self.search_calls.append((query, mode, limit))
        return [
            KnowledgeSearchResult(
                chunk_key=f"chunk-{mode}",
                source_id="knowledge-git",
                source_path="economics/inflation.md",
                heading_path="Inflation > Kernidee",
                content="Inflation > Kernidee\n\nContent.",
                score=0.75,
                retrieval_mode=mode,  # type: ignore[arg-type]
            )
        ]

    def get_document(self, source_path: str, source_id: str) -> SourceDocument:
        self.document_calls.append((source_id, source_path))
        return SourceDocument(
            source_id=source_id,
            source_path=source_path,
            content="# Inflation\n\nOriginal document.",
        )


class FakeSource:
    source_id = "knowledge-git"

    def discover(self) -> list[SourceDocument]:
        return []

    def get_document(self, source_path: str) -> SourceDocument:
        return SourceDocument(
            source_id=self.source_id,
            source_path=source_path,
            content="# Inflation\n\nOriginal document.",
        )


class FakeReranker:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def rerank(
        self,
        query: str,
        candidates: list[SearchResult],
        limit: int,
    ) -> list[SearchResult]:
        self.calls.append(query)
        return candidates[:limit]


def _call(coro):
    return asyncio.run(coro)


def _result_text(result: Any) -> str:
    return "\n".join(block.text for block in result.content if isinstance(block, TextContent))


def _settings() -> Settings:
    return Settings(
        database_url="postgresql://example",
        knowledge_root=Path("knowledge"),
        embedding_model="model",
        embedding_dimensions=384,
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _running_http_server(
    server: MCPServer,
    path: str = "/mcp",
    token: str | None = None,
) -> Iterator[str]:
    host = "127.0.0.1"
    port = _free_port()
    security = create_loopback_transport_security(host, port)
    if token is None:
        app = server.streamable_http_app(
            streamable_http_path=path, host=host, transport_security=security
        )
    else:
        app = create_http_app(
            server,
            MCPServerSettings(
                transport="streamable-http",
                host=host,
                port=port,
                path=path,
                http_client_id="test-client",
                http_bearer_token=token,
            ),
            security,
        )
    uvicorn_server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="error",
            access_log=False,
        )
    )
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not uvicorn_server.started:
        if not thread.is_alive():
            raise RuntimeError("Streamable HTTP test server stopped during startup")
        if time.monotonic() >= deadline:
            raise RuntimeError("Streamable HTTP test server did not start")
        time.sleep(0.01)

    try:
        yield f"http://{host}:{port}"
    finally:
        uvicorn_server.should_exit = True
        thread.join(timeout=10)
        if thread.is_alive():
            raise RuntimeError("Streamable HTTP test server did not stop")


async def _list_stdio_tools():
    project_root = Path(__file__).resolve().parents[1]
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "knowledge_system.mcp_server"],
        cwd=project_root,
        env={
            "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
            "MCP_TRANSPORT": "stdio",
        },
    )
    async with Client(stdio_client(server)) as client:
        return await client.list_tools()


def test_mcp_server_lists_exactly_two_read_only_tools() -> None:
    async def scenario():
        async with Client(create_mcp_server(FakeKnowledgeService())) as client:
            return await client.list_tools()

    listed = _call(scenario())

    assert [tool.name for tool in listed.tools] == ["search_knowledge", "get_document"]
    assert all(tool.annotations is not None for tool in listed.tools)
    assert all(tool.annotations.read_only_hint is True for tool in listed.tools)
    assert all(tool.annotations.open_world_hint is False for tool in listed.tools)
    assert "before answering" in listed.tools[0].description
    assert "canonical original document" in listed.tools[1].description


def test_mcp_server_settings_default_to_stdio_and_local_http(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    for name in (
        "MCP_TRANSPORT",
        "MCP_HOST",
        "MCP_PORT",
        "MCP_PATH",
        "MCP_ALLOWED_HOSTS",
        "MCP_ALLOWED_ORIGINS",
        "MCP_HTTP_CLIENT_ID",
        "MCP_HTTP_BEARER_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = get_mcp_server_settings()

    assert settings == MCPServerSettings(
        transport="stdio",
        host="127.0.0.1",
        port=8000,
        path="/mcp",
        allowed_hosts=(),
        allowed_origins=(),
    )


def test_mcp_server_settings_read_streamable_http_environment(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("MCP_HOST", "localhost")
    monkeypatch.setenv("MCP_PORT", "8123")
    monkeypatch.setenv("MCP_PATH", "/knowledge")
    monkeypatch.setenv("MCP_HTTP_CLIENT_ID", "test-client")
    monkeypatch.setenv("MCP_HTTP_BEARER_TOKEN", "a" * 32)

    settings = get_mcp_server_settings()

    assert settings == MCPServerSettings(
        transport="streamable-http",
        host="localhost",
        port=8123,
        path="/knowledge",
        http_client_id="test-client",
        http_bearer_token="a" * 32,
    )


def test_mcp_server_settings_read_explicit_transport_allowlists(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MCP_HOST", "0.0.0.0")
    monkeypatch.setenv(
        "MCP_ALLOWED_HOSTS",
        "127.0.0.1:8000, localhost:8000",
    )
    monkeypatch.setenv(
        "MCP_ALLOWED_ORIGINS",
        "http://127.0.0.1:8000, http://localhost:8000",
    )

    settings = get_mcp_server_settings()

    assert settings.allowed_hosts == ("127.0.0.1:8000", "localhost:8000")
    assert settings.allowed_origins == (
        "http://127.0.0.1:8000",
        "http://localhost:8000",
    )


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("MCP_TRANSPORT", "sse", "Unsupported MCP_TRANSPORT"),
        ("MCP_PORT", "invalid", "MCP_PORT must be an integer"),
        ("MCP_PORT", "70000", "MCP_PORT must be between"),
        ("MCP_PATH", "mcp", "MCP_PATH must be an absolute URL path"),
        ("MCP_ALLOWED_HOSTS", "127.0.0.1:*", "must not contain wildcards"),
        ("MCP_ALLOWED_ORIGINS", "http://localhost:*", "must not contain wildcards"),
    ],
)
def test_mcp_server_settings_reject_invalid_values(
    monkeypatch,
    tmp_path: Path,
    name: str,
    value: str,
    message: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=message):
        get_mcp_server_settings()


def test_mcp_server_settings_reject_origins_without_hosts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "http://127.0.0.1:8000")

    with pytest.raises(ValueError, match="requires MCP_ALLOWED_HOSTS"):
        get_mcp_server_settings()


def test_run_mcp_server_keeps_stdio_as_default(monkeypatch) -> None:
    class FakeServer:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def run(self, transport: str, **kwargs: object) -> None:
            self.calls.append((transport, kwargs))

    server = FakeServer()
    monkeypatch.setattr("knowledge_system.mcp_server.create_mcp_server", lambda service: server)

    run_mcp_server(
        MCPServerSettings(
            transport="stdio",
            host="127.0.0.1",
            port=8000,
            path="/mcp",
        )
    )

    assert server.calls == [("stdio", {})]


def test_run_mcp_server_passes_http_options_and_security(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        "knowledge_system.mcp_server.uvicorn.run",
        lambda app, **kwargs: calls.append((app, kwargs)),
    )
    settings = MCPServerSettings(
        transport="streamable-http",
        host="127.0.0.1",
        port=8123,
        path="/knowledge",
        http_client_id="test-client",
        http_bearer_token="a" * 32,
    )

    run_mcp_server(settings, service=FakeKnowledgeService())

    app, options = calls[0]
    security = create_transport_security(settings)
    assert app.client_id == "test-client"
    assert app.token == "a" * 32
    assert options == {"host": "127.0.0.1", "port": 8123, "access_log": False}
    assert any(route.path == "/knowledge" for route in app.app.routes)
    assert isinstance(security, TransportSecuritySettings)
    assert security.enable_dns_rebinding_protection is True
    assert "127.0.0.1:8123" in security.allowed_hosts
    assert not any("*" in host for host in security.allowed_hosts)
    assert "evil.example" not in security.allowed_hosts


def test_non_loopback_host_requires_explicit_allowlist(monkeypatch) -> None:
    settings = MCPServerSettings(
        transport="streamable-http",
        host="0.0.0.0",
        port=8000,
        path="/mcp",
        http_client_id="test-client",
        http_bearer_token="a" * 32,
    )

    with pytest.raises(ValueError, match="explicit TransportSecuritySettings"):
        run_mcp_server(settings, service=FakeKnowledgeService())


def test_non_loopback_host_accepts_explicit_allowlist(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        "knowledge_system.mcp_server.uvicorn.run",
        lambda app, **kwargs: calls.append((app, kwargs)),
    )
    settings = MCPServerSettings(
        transport="streamable-http",
        host="0.0.0.0",
        port=8000,
        path="/mcp",
        http_client_id="test-client",
        http_bearer_token="a" * 32,
    )
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["mcp.example.test", "mcp.example.test:*"],
        allowed_origins=["https://mcp.example.test"],
    )

    run_mcp_server(settings, service=FakeKnowledgeService(), transport_security=security)

    assert calls[0][1]["host"] == "0.0.0.0"


def test_non_loopback_host_accepts_configured_allowlist(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        "knowledge_system.mcp_server.uvicorn.run",
        lambda app, **kwargs: calls.append((app, kwargs)),
    )
    settings = MCPServerSettings(
        transport="streamable-http",
        host="0.0.0.0",
        port=8000,
        path="/mcp",
        allowed_hosts=("127.0.0.1:8000", "localhost:8000"),
        allowed_origins=(
            "http://127.0.0.1:8000",
            "http://localhost:8000",
        ),
        http_client_id="test-client",
        http_bearer_token="a" * 32,
    )

    run_mcp_server(settings, service=FakeKnowledgeService())

    security = create_transport_security(settings)
    assert isinstance(security, TransportSecuritySettings)
    assert security.allowed_hosts == ["127.0.0.1:8000", "localhost:8000"]
    assert calls[0][1]["access_log"] is False


def test_search_knowledge_schema_and_structured_fast_quality_results() -> None:
    service = FakeKnowledgeService()

    async def scenario():
        async with Client(create_mcp_server(service)) as client:
            listed = await client.list_tools()
            fast = await client.call_tool(
                "search_knowledge",
                {"query": "  Inflation  ", "mode": "fast", "limit": 3},
            )
            quality = await client.call_tool("search_knowledge", {"query": "Realzinsen"})
            return listed.tools[0], fast, quality

    tool, fast, quality = _call(scenario())

    assert tool.input_schema["properties"]["mode"]["enum"] == ["fast", "quality"]
    assert tool.input_schema["properties"]["mode"]["default"] == "quality"
    assert tool.input_schema["properties"]["limit"]["default"] == 5
    assert set(tool.output_schema["$defs"]["SearchKnowledgeItem"]["properties"]) >= {
        "chunk_key",
        "source_id",
        "source_path",
        "heading_path",
        "content",
        "score",
        "retrieval_mode",
    }
    assert fast.is_error is False
    assert quality.is_error is False
    assert fast.structured_content == {
        "results": [
            {
                "chunk_key": "chunk-fast",
                "source_id": "knowledge-git",
                "source_path": "economics/inflation.md",
                "heading_path": "Inflation > Kernidee",
                "content": "Inflation > Kernidee\n\nContent.",
                "score": 0.75,
                "retrieval_mode": "fast",
            }
        ]
    }
    assert quality.structured_content["results"][0]["retrieval_mode"] == "quality"
    assert service.search_calls == [
        ("Inflation", "fast", 3),
        ("Realzinsen", "quality", 5),
    ]


def test_get_document_uses_service_and_returns_original_document() -> None:
    service = FakeKnowledgeService()

    async def scenario():
        async with Client(create_mcp_server(service)) as client:
            return await client.call_tool(
                "get_document",
                {
                    "source_id": "knowledge-git",
                    "source_path": "economics/inflation.md",
                },
            )

    result = _call(scenario())

    assert result.is_error is False
    assert result.structured_content == {
        "source_id": "knowledge-git",
        "source_path": "economics/inflation.md",
        "content": "# Inflation\n\nOriginal document.",
    }
    assert service.document_calls == [("knowledge-git", "economics/inflation.md")]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"query": "   "}, "query must not be empty"),
        ({"query": "Inflation", "mode": "unknown"}, "fast"),
        ({"query": "Inflation", "limit": 0}, "limit must be between 1 and 20"),
        ({"query": "Inflation", "limit": 21}, "limit must be between 1 and 20"),
    ],
)
def test_search_knowledge_rejects_invalid_input(arguments: dict[str, object], message: str) -> None:
    service = FakeKnowledgeService()

    async def scenario():
        async with Client(create_mcp_server(service)) as client:
            return await client.call_tool("search_knowledge", arguments)

    result = _call(scenario())

    assert result.is_error is True
    assert message in _result_text(result)
    assert service.search_calls == []


def test_mcp_errors_do_not_expose_database_details() -> None:
    class UnavailableService(FakeKnowledgeService):
        def search(self, query: str, mode: str, limit: int) -> list[KnowledgeSearchResult]:
            try:
                raise RuntimeError("postgresql://user:secret@internal-host/private")
            except RuntimeError as exc:
                raise KnowledgeIndexUnavailableError("Knowledge index is unavailable") from exc

    async def scenario():
        async with Client(create_mcp_server(UnavailableService())) as client:
            return await client.call_tool("search_knowledge", {"query": "Inflation"})

    result = _call(scenario())
    text = _result_text(result)

    assert result.is_error is True
    assert "PostgreSQL is running" in text
    assert "secret" not in text
    assert "internal-host" not in text


def test_get_document_reports_unknown_source_and_path() -> None:
    class MissingService(FakeKnowledgeService):
        def get_document(self, source_path: str, source_id: str) -> SourceDocument:
            if source_id == "missing":
                raise UnknownSourceError("Unknown source_id `missing`")
            raise SourceDocumentNotFoundError(
                f"Unknown source_path `{source_path}` for source_id `{source_id}`"
            )

    async def scenario():
        async with Client(create_mcp_server(MissingService())) as client:
            unknown_source = await client.call_tool(
                "get_document",
                {"source_id": "missing", "source_path": "economics/inflation.md"},
            )
            unknown_path = await client.call_tool(
                "get_document",
                {"source_id": "knowledge-git", "source_path": "economics/missing.md"},
            )
            return unknown_source, unknown_path

    unknown_source, unknown_path = _call(scenario())

    assert unknown_source.is_error is True
    assert "Unknown source_id" in _result_text(unknown_source)
    assert unknown_path.is_error is True
    assert "Unknown source_path" in _result_text(unknown_path)


@pytest.mark.parametrize("source_path", ["../../secret.txt", "C:\\secret.txt", "/etc/passwd"])
def test_get_document_rejects_path_traversal_and_absolute_paths(
    tmp_path: Path,
    source_path: str,
) -> None:
    knowledge_root = tmp_path / "corpus" / "knowledge"
    knowledge_root.mkdir(parents=True)
    (knowledge_root / "valid.md").write_text("# Valid\n\nAllowed.", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("must not be returned", encoding="utf-8")
    settings = Settings(
        database_url="postgresql://example",
        knowledge_root=knowledge_root,
        embedding_model="model",
        embedding_dimensions=384,
    )
    service = KnowledgeService(
        settings,
        sources=[GitMarkdownSource(knowledge_root)],
        verbose=False,
    )

    async def scenario():
        async with Client(create_mcp_server(service)) as client:
            return await client.call_tool(
                "get_document",
                {"source_id": "knowledge-git", "source_path": source_path},
            )

    result = _call(scenario())

    assert result.is_error is True
    assert "must not be returned" not in _result_text(result)


def test_stdio_entrypoint_starts_and_lists_tools() -> None:
    listed = _call(_list_stdio_tools())

    assert [tool.name for tool in listed.tools] == ["search_knowledge", "get_document"]


def test_streamable_http_uses_same_tools_service_and_lazy_reranker(monkeypatch) -> None:
    def fake_keyword_search(settings, query, limit, text_config, verbose):
        return [
            SearchResult(
                chunk_key=f"chunk-{query}",
                source_id="knowledge-git",
                source_path="economics/inflation.md",
                heading_path="Inflation > Kernidee",
                content="Inflation > Kernidee\n\nContent.",
                similarity=0.75,
            )
        ]

    rerankers: list[FakeReranker] = []

    def fake_reranker_factory(verbose: bool) -> FakeReranker:
        reranker = FakeReranker()
        rerankers.append(reranker)
        return reranker

    monkeypatch.setattr("knowledge_system.service.keyword_search", fake_keyword_search)
    monkeypatch.setattr("knowledge_system.service.LocalReranker", fake_reranker_factory)
    service = KnowledgeService(
        _settings(),
        sources=[FakeSource()],
        verbose=False,
    )
    server = create_mcp_server(service)

    async def scenario(url: str):
        async with Client(f"{url}/mcp", read_timeout_seconds=10) as client:
            listed = await client.list_tools()
            fast = await client.call_tool(
                "search_knowledge",
                {"query": "Inflation", "mode": "fast", "limit": 5},
            )
            quality_first = await client.call_tool(
                "search_knowledge",
                {"query": "Realzinsen", "mode": "quality", "limit": 5},
            )
            quality_second = await client.call_tool(
                "search_knowledge",
                {"query": "Geldpolitik", "mode": "quality", "limit": 5},
            )
            document = await client.call_tool(
                "get_document",
                {
                    "source_id": "knowledge-git",
                    "source_path": "economics/inflation.md",
                },
            )
        async with httpx2.AsyncClient() as http_client:
            invalid_host = await http_client.get(
                f"{url}/mcp",
                headers={"Host": "evil.example", "Accept": "text/event-stream"},
            )
            wrong_path = await http_client.get(f"{url}/wrong")
        return listed, fast, quality_first, quality_second, document, invalid_host, wrong_path

    with _running_http_server(server) as url:
        http_results = _call(scenario(url))
    stdio_tools = _call(_list_stdio_tools())
    listed, fast, quality_first, quality_second, document, invalid_host, wrong_path = http_results

    assert [tool.name for tool in listed.tools] == ["search_knowledge", "get_document"]
    assert [(tool.name, tool.input_schema, tool.output_schema) for tool in listed.tools] == [
        (tool.name, tool.input_schema, tool.output_schema) for tool in stdio_tools.tools
    ]
    assert fast.is_error is False
    assert fast.structured_content["results"][0]["retrieval_mode"] == "fast"
    assert quality_first.structured_content["results"][0]["retrieval_mode"] == "quality"
    assert quality_second.structured_content["results"][0]["retrieval_mode"] == "quality"
    assert document.structured_content == {
        "source_id": "knowledge-git",
        "source_path": "economics/inflation.md",
        "content": "# Inflation\n\nOriginal document.",
    }
    assert len(rerankers) == 1
    assert rerankers[0].calls == ["Realzinsen", "Geldpolitik"]
    assert invalid_host.status_code == 421
    assert invalid_host.text == "Invalid Host header"
    assert wrong_path.status_code == 404


@pytest.mark.parametrize(
    ("client_id", "token"),
    [("", "a" * 32), ("machine", ""), ("machine", "short"), ("bad id", "a" * 32)],
)
def test_http_settings_fail_closed_without_machine_credentials(
    monkeypatch, tmp_path: Path, client_id: str, token: str
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("MCP_HTTP_CLIENT_ID", client_id)
    monkeypatch.setenv("MCP_HTTP_BEARER_TOKEN", token)
    with pytest.raises(ValueError, match="MCP_HTTP_CLIENT_ID"):
        get_mcp_server_settings()


def test_authenticated_http_limits_tools_sources_and_paths(tmp_path: Path, monkeypatch) -> None:
    token = "s" * 32
    knowledge_root = tmp_path / "knowledge"
    (knowledge_root / "economics").mkdir(parents=True)
    (knowledge_root / "economics" / "inflation.md").write_text(
        "# Synthetic inflation\n\nOriginal synthetic document.", encoding="utf-8"
    )
    (knowledge_root / "economics" / "missing.txt").write_text(
        "Private synthetic text", encoding="utf-8"
    )

    queries = []

    def fake_keyword_search(settings, query, limit, text_config, verbose, source_ids=None):
        queries.append(source_ids)
        return [
            SearchResult(
                chunk_key="allowed",
                source_id="knowledge-git",
                source_path="economics/inflation.md",
                heading_path="Synthetic",
                content="Allowed synthetic chunk",
                similarity=0.8,
            ),
            SearchResult(
                chunk_key="forbidden",
                source_id="private-source",
                source_path="secret.md",
                heading_path="Private",
                content="Forbidden synthetic chunk",
                similarity=0.9,
            ),
        ]

    monkeypatch.setattr("knowledge_system.service.keyword_search", fake_keyword_search)
    settings = Settings(
        database_url="postgresql://example",
        knowledge_root=knowledge_root,
        embedding_model="model",
        embedding_dimensions=384,
    )
    service = KnowledgeService(settings, sources=[GitMarkdownSource(knowledge_root)], verbose=False)
    server = create_mcp_server(service, allowed_source_ids=frozenset({"knowledge-git"}))

    async def scenario(url: str):
        async with (
            httpx2.AsyncClient(
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-MCP-Client-ID": "forged-admin",
                    "X-MCP-Source-IDs": "private-source",
                }
            ) as http_client,
            Client(
                streamable_http_client(f"{url}/mcp", http_client=http_client), mode="legacy"
            ) as client,
        ):
            listed = await client.list_tools()
            search = await client.call_tool(
                "search_knowledge", {"query": "synthetic", "mode": "fast"}
            )
            document = await client.call_tool(
                "get_document",
                {"source_id": "knowledge-git", "source_path": "economics/inflation.md"},
            )
            rejected = []
            for source_id, source_path in [
                ("unknown", "economics/inflation.md"),
                ("private-source", "secret.md"),
                ("knowledge-git", "economics/missing.md"),
                ("knowledge-git", "economics/missing.txt"),
                ("knowledge-git", "../secret.md"),
                ("knowledge-git", "/etc/passwd"),
            ]:
                rejected.append(
                    await client.call_tool(
                        "get_document", {"source_id": source_id, "source_path": source_path}
                    )
                )
            unavailable = await client.call_tool("apply_accepted_revision", {})
            return listed, search, document, rejected, unavailable

    with _running_http_server(server, token=token) as url:
        listed, search, document, rejected, unavailable = _call(scenario(url))

    assert [tool.name for tool in listed.tools] == ["search_knowledge", "get_document"]
    assert queries == [frozenset({"knowledge-git"})]
    assert search.structured_content["results"][0]["source_id"] == "knowledge-git"
    assert len(search.structured_content["results"]) == 1
    assert "Forbidden synthetic chunk" not in str(search.structured_content)
    assert document.structured_content == {
        "source_id": "knowledge-git",
        "source_path": "economics/inflation.md",
        "content": "# Synthetic inflation\n\nOriginal synthetic document.",
    }
    assert all(result.is_error for result in rejected)
    assert all("Original synthetic document" not in _result_text(result) for result in rejected)
    assert unavailable.is_error is True


def test_http_auth_rejects_missing_wrong_duplicate_and_alternate_credentials() -> None:
    token = "s" * 32
    service = FakeKnowledgeService()
    server = create_mcp_server(service, allowed_source_ids=frozenset({"knowledge-git"}))

    async def scenario(url: str):
        responses = []
        cases = [
            ({}, ""),
            ({"Authorization": "Bearer " + "x" * 32}, ""),
            ({"Authorization": "Basic " + token}, ""),
            ({"Authorization": "Bearer " + token + ", Bearer " + token}, ""),
            ({"Authorization": "Bearer " + token, "X-Api-Key": "other"}, ""),
            ({"Authorization": "Bearer " + token}, "?access_token=other"),
            ({"X-MCP-Client-ID": "test-client", "X-MCP-Source-IDs": "knowledge-git"}, ""),
        ]
        async with httpx2.AsyncClient() as client:
            for headers, suffix in cases:
                responses.append(
                    await client.post(
                        f"{url}/mcp{suffix}",
                        headers=headers,
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    )
                )
            duplicate = await client.post(
                f"{url}/mcp",
                headers=[("Authorization", f"Bearer {token}"), ("Authorization", "Bearer other")],
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
            forbidden_document = await client.post(
                f"{url}/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "get_document",
                        "arguments": {
                            "source_id": "knowledge-git",
                            "source_path": "economics/inflation.md",
                        },
                    },
                },
            )
        return responses + [duplicate, forbidden_document]

    with _running_http_server(server, token=token) as url:
        responses = _call(scenario(url))
    assert all(response.status_code == 401 for response in responses)
    assert all(response.text == "Unauthorized" for response in responses)
    assert all(response.headers["www-authenticate"] == "Bearer" for response in responses)
    assert service.search_calls == []
    assert service.document_calls == []
