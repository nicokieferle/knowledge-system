from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

from knowledge_system.config import Settings
from knowledge_system.mcp_server import create_mcp_server
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

    def search(self, query: str, mode: str, limit: int) -> list[KnowledgeSearchResult]:
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


def _call(coro):
    return asyncio.run(coro)


def _result_text(result: Any) -> str:
    return "\n".join(
        block.text for block in result.content if isinstance(block, TextContent)
    )


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
    project_root = Path(__file__).resolve().parents[1]
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "knowledge_system.mcp_server"],
        cwd=project_root,
        env={"PYTHONPATH": os.environ.get("PYTHONPATH", "")},
    )

    async def scenario():
        async with Client(stdio_client(server)) as client:
            return await client.list_tools()

    listed = _call(scenario())

    assert [tool.name for tool in listed.tools] == ["search_knowledge", "get_document"]
