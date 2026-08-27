from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any

from knowledge_system.config import Settings
from knowledge_system.search import SearchResult, keyword_search, reciprocal_rank_fusion, rrf_score


class FakeRows:
    def __init__(self, rows: list[tuple[str, str, str, str, str, float]]) -> None:
        self.rows = rows

    def fetchall(self) -> list[tuple[str, str, str, str, str, float]]:
        return self.rows


class FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, params: tuple[Any, ...]) -> FakeRows:
        self.calls.append((sql, params))
        return FakeRows(
            [
                (
                    "chunk-inflation-1",
                    "knowledge-git",
                    "economics/inflation.md",
                    "Inflation > Angebotsschocks",
                    "Inflation > Angebotsschocks\n\nEngpässe und Energiepreise.",
                    0.42,
                )
            ]
        )


def _settings() -> Settings:
    return Settings(
        database_url="postgresql://example",
        knowledge_root=Path("knowledge"),
        embedding_model="model",
        embedding_dimensions=384,
    )


def test_keyword_search_returns_ranked_results_for_german_terms(monkeypatch) -> None:
    fake_conn = FakeConnection()

    @contextmanager
    def fake_connect(settings: Settings):
        yield fake_conn

    monkeypatch.setattr("knowledge_system.search.connect", fake_connect)

    results = keyword_search(
        _settings(),
        "Welche Rolle spielen Engpässe und Energiepreise für Inflation?",
        text_config="german",
        verbose=False,
    )

    assert results[0].source_path == "economics/inflation.md"
    assert results[0].source_id == "knowledge-git"
    assert results[0].heading_path == "Inflation > Angebotsschocks"
    assert results[0].similarity == 0.42
    assert fake_conn.calls[0][1][:4] == (
        "german",
        "Welche Rolle spielen Engpässe und Energiepreise für Inflation?",
        "german",
        "german",
    )


def test_keyword_search_handles_empty_query_without_sql(monkeypatch) -> None:
    fake_conn = FakeConnection()

    @contextmanager
    def fake_connect(settings: Settings):
        yield fake_conn

    monkeypatch.setattr("knowledge_system.search.connect", fake_connect)

    assert keyword_search(_settings(), "   ", verbose=False) == []
    assert fake_conn.calls == []


def test_keyword_search_accepts_unusual_keyword_query(monkeypatch) -> None:
    fake_conn = FakeConnection()

    @contextmanager
    def fake_connect(settings: Settings):
        yield fake_conn

    monkeypatch.setattr("knowledge_system.search.connect", fake_connect)

    results = keyword_search(_settings(), '"Realzins" OR +Inflation -Assetpreise', verbose=False)

    assert len(results) == 1
    assert fake_conn.calls[0][1][1] == '"Realzins" OR +Inflation -Assetpreise'


def test_rrf_score_uses_expected_formula() -> None:
    assert rrf_score(1) == 1 / 61
    assert rrf_score(20) == 1 / 80


def test_rrf_rejects_invalid_rank() -> None:
    try:
        rrf_score(0)
    except ValueError as exc:
        assert "at least 1" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_rrf_uses_chunk_identity_across_retrievers() -> None:
    vector_result = _search_result(
        "chunk-a",
        "economics/a.md",
        "A > One",
        0.9,
    )
    keyword_result = _search_result(
        "chunk-a",
        "economics/a.md",
        "A > One",
        0.2,
    )

    fused = reciprocal_rank_fusion([[vector_result], [keyword_result]], limit=5)

    assert len(fused) == 1
    assert fused[0].chunk_key == "chunk-a"
    assert fused[0].similarity == rrf_score(1) + rrf_score(1)


def test_rrf_tie_breaks_are_deterministic() -> None:
    b_result = _search_result("chunk-b", "economics/b.md", "B", 0.9)
    a_result = _search_result("chunk-a", "economics/a.md", "A", 0.8)

    fused = reciprocal_rank_fusion([[b_result], [a_result]], limit=5)

    assert [result.source_path for result in fused] == [
        "economics/a.md",
        "economics/b.md",
    ]


def _search_result(
    chunk_key: str,
    source_path: str,
    heading_path: str,
    similarity: float,
) -> SearchResult:
    return SearchResult(
        chunk_key=chunk_key,
        source_path=source_path,
        heading_path=heading_path,
        content=f"{heading_path}\n\ncontent",
        similarity=similarity,
    )
