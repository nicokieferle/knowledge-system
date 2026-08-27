from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from knowledge_system.config import Settings
from knowledge_system.search import SearchResult
from knowledge_system.service import (
    KnowledgeIndexUnavailableError,
    KnowledgeService,
    UnknownSourceError,
)
from knowledge_system.sources import SourceDocument


class FakeSource:
    source_id = "economics-git"

    def __init__(self) -> None:
        self.requests: list[str] = []

    def discover(self) -> list[SourceDocument]:
        return []

    def get_document(self, source_path: str) -> SourceDocument:
        self.requests.append(source_path)
        return SourceDocument(
            source_id=self.source_id,
            source_path=source_path,
            content="# Original\n\nFrom source.",
            metadata={},
        )


class FakeReranker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...], int]] = []

    def rerank(
        self,
        query: str,
        candidates: list[SearchResult],
        limit: int = 5,
    ) -> list[SearchResult]:
        self.calls.append((query, tuple(candidate.chunk_key for candidate in candidates), limit))
        return list(reversed(candidates))[:limit]


def _settings() -> Settings:
    return Settings(
        database_url="postgresql://example",
        knowledge_root=Path("knowledge"),
        embedding_model="model",
        embedding_dimensions=384,
    )


def _search_result(
    chunk_key: str,
    source_id: str = "economics-git",
) -> SearchResult:
    return SearchResult(
        chunk_key=chunk_key,
        source_id=source_id,
        source_path="economics/inflation.md",
        heading_path="Inflation > Kernidee",
        content="Inflation > Kernidee\n\nContent.",
        similarity=0.5,
    )


def test_knowledge_service_search_fast_uses_keyword_german(monkeypatch) -> None:
    calls: list[tuple[str, int, str]] = []

    def fake_keyword_search(settings, query, limit, text_config, verbose):
        calls.append((query, limit, text_config))
        return [_search_result("chunk-fast")]

    monkeypatch.setattr("knowledge_system.service.keyword_search", fake_keyword_search)
    service = KnowledgeService(_settings(), sources=[FakeSource()], verbose=False)

    results = service.search("query", mode="fast", limit=5)

    assert calls == [("query", 5, "german")]
    assert results[0].chunk_key == "chunk-fast"
    assert results[0].source_id == "economics-git"
    assert results[0].retrieval_mode == "fast"


def test_knowledge_service_search_quality_reranks_keyword_top_10(monkeypatch) -> None:
    calls: list[tuple[str, int, str]] = []

    def fake_keyword_search(settings, query, limit, text_config, verbose):
        calls.append((query, limit, text_config))
        return [_search_result("chunk-a"), _search_result("chunk-b")]

    reranker = FakeReranker()
    monkeypatch.setattr("knowledge_system.service.keyword_search", fake_keyword_search)
    service = KnowledgeService(
        _settings(),
        sources=[FakeSource()],
        reranker=reranker,
        verbose=False,
    )

    results = service.search("query", mode="quality", limit=5)

    assert calls == [("query", 10, "german")]
    assert reranker.calls == [("query", ("chunk-a", "chunk-b"), 5)]
    assert [result.chunk_key for result in results] == ["chunk-b", "chunk-a"]
    assert all(result.retrieval_mode == "quality" for result in results)


def test_knowledge_service_reuses_lazy_reranker(monkeypatch) -> None:
    def fake_keyword_search(settings, query, limit, text_config, verbose):
        return [_search_result(f"chunk-{query}")]

    created_rerankers: list[FakeReranker] = []

    def fake_reranker_factory(verbose: bool) -> FakeReranker:
        reranker = FakeReranker()
        created_rerankers.append(reranker)
        return reranker

    monkeypatch.setattr("knowledge_system.service.keyword_search", fake_keyword_search)
    monkeypatch.setattr("knowledge_system.service.LocalReranker", fake_reranker_factory)
    service = KnowledgeService(_settings(), sources=[FakeSource()], verbose=False)

    service.search("first", mode="quality", limit=5)
    service.search("second", mode="quality", limit=5)

    assert len(created_rerankers) == 1
    assert [call[0] for call in created_rerankers[0].calls] == ["first", "second"]


def test_knowledge_service_translates_database_failure(monkeypatch) -> None:
    def fake_keyword_search(settings, query, limit, text_config, verbose):
        raise psycopg.OperationalError("internal database detail")

    monkeypatch.setattr("knowledge_system.service.keyword_search", fake_keyword_search)
    service = KnowledgeService(_settings(), sources=[FakeSource()], verbose=False)

    with pytest.raises(KnowledgeIndexUnavailableError, match="Knowledge index is unavailable"):
        service.search("query", mode="fast")


def test_knowledge_service_rejects_unknown_mode() -> None:
    service = KnowledgeService(_settings(), sources=[FakeSource()], verbose=False)

    with pytest.raises(ValueError, match="Unknown retrieval mode"):
        service.search("query", mode="unknown")  # type: ignore[arg-type]


def test_knowledge_service_get_document_reads_original_source() -> None:
    source = FakeSource()
    service = KnowledgeService(_settings(), sources=[source], verbose=False)

    document = service.get_document("economics/inflation.md", source_id="economics-git")

    assert source.requests == ["economics/inflation.md"]
    assert document.source_id == "economics-git"
    assert document.content == "# Original\n\nFrom source."


def test_knowledge_service_rejects_unknown_source() -> None:
    service = KnowledgeService(_settings(), sources=[FakeSource()], verbose=False)

    with pytest.raises(UnknownSourceError, match="Unknown source_id"):
        service.get_document("economics/inflation.md", source_id="missing")


def test_knowledge_service_keeps_source_id_in_results(monkeypatch) -> None:
    def fake_keyword_search(settings, query, limit, text_config, verbose):
        return [_search_result("chunk-trading", source_id="trading")]

    monkeypatch.setattr("knowledge_system.service.keyword_search", fake_keyword_search)
    service = KnowledgeService(_settings(), sources=[FakeSource()], verbose=False)

    result = service.search("query", mode="fast", limit=1)[0]

    assert result.source_id == "trading"
    assert result.source_path == "economics/inflation.md"
    assert result.score == 0.5
