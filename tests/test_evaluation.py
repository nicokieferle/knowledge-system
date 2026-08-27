from __future__ import annotations

from pathlib import Path

import pytest

from knowledge_system.config import Settings
from knowledge_system.evaluation import (
    EvalCase,
    EvalReport,
    build_hybrid_search,
    build_keyword_search,
    build_reusable_embedder_search,
    build_search,
    evaluate_cases,
    format_human_report,
    run_retrieval_eval,
)
from knowledge_system.search import SearchResult


def _result(
    source_path: str,
    heading_path: str,
    similarity: float,
    chunk_key: str = "",
) -> SearchResult:
    return SearchResult(
        chunk_key=chunk_key,
        source_path=source_path,
        heading_path=heading_path,
        content="content",
        similarity=similarity,
    )


def test_evaluate_cases_computes_hit_rates_and_mrr() -> None:
    cases = [
        EvalCase(
            id="C-1",
            query="first",
            expected_sources=("a.md",),
            expected_headings=("A > One",),
        ),
        EvalCase(id="C-2", query="second", expected_sources=("b.md",)),
        EvalCase(
            id="C-3",
            query="third",
            expected_sources=("c.md",),
            expected_headings=("C > Expected",),
        ),
    ]
    search_results = {
        "first": [_result("a.md", "A > One", 0.9)],
        "second": [
            _result("x.md", "X", 0.8),
            _result("b.md", "B > Any heading", 0.7),
        ],
        "third": [_result("c.md", "C > Wrong", 0.95)],
    }

    case_results = evaluate_cases(cases, lambda query, limit: search_results[query], limit=5)
    report = EvalReport(suite_path=Path("suite.jsonl"), limit=5, case_results=case_results)

    assert report.hit_rate_at(1) == pytest.approx(1 / 3)
    assert report.hit_rate_at(3) == pytest.approx(2 / 3)
    assert report.hit_rate_at(5) == pytest.approx(2 / 3)
    assert report.mrr == pytest.approx(0.5)
    assert case_results[2].first_relevant_rank is None


def test_evaluate_cases_requires_limit_for_hit_at_5() -> None:
    cases = [EvalCase(id="C-1", query="query", expected_sources=("a.md",))]

    with pytest.raises(ValueError, match="Hit@5"):
        evaluate_cases(cases, lambda query, limit: [], limit=3)


def test_format_human_report_shows_failure_details() -> None:
    case = EvalCase(
        id="C-1",
        query="query",
        expected_sources=("expected.md",),
        expected_headings=("Expected heading",),
        description="why",
    )
    case_results = evaluate_cases(
        [case],
        lambda query, limit: [_result("actual.md", "Actual heading", 0.42)],
        limit=5,
    )
    report = EvalReport(suite_path=Path("suite.jsonl"), limit=5, case_results=case_results)

    output = format_human_report(report)

    assert "[FAIL] C-1 rank=miss" in output
    assert "expected.md" in output
    assert "Expected heading" in output
    assert "actual.md" in output
    assert "similarity=0.4200" in output


def test_reusable_eval_search_builds_one_embedder_for_multiple_queries() -> None:
    settings = Settings(
        database_url="postgresql://example",
        knowledge_root=Path("knowledge"),
        embedding_model="model",
        embedding_dimensions=384,
    )
    created_embedders: list[object] = []
    used_embedders: list[object] = []

    def embedder_factory(model_name: str, dimensions: int, verbose: bool) -> object:
        embedder = object()
        created_embedders.append(embedder)
        assert model_name == "model"
        assert dimensions == 384
        assert verbose is False
        return embedder

    def search_with_embedder(
        passed_settings: Settings,
        query: str,
        embedder: object,
        limit: int,
        verbose: bool,
    ) -> list[SearchResult]:
        assert passed_settings is settings
        assert limit == 5
        assert verbose is False
        used_embedders.append(embedder)
        return [_result(f"{query}.md", "Heading", 0.9)]

    search = build_reusable_embedder_search(
        settings,
        verbose=False,
        search_with_embedder=search_with_embedder,
        embedder_factory=embedder_factory,
    )

    assert search("first", 5)[0].source_path == "first.md"
    assert search("second", 5)[0].source_path == "second.md"
    assert len(created_embedders) == 1
    assert used_embedders == [created_embedders[0], created_embedders[0]]


def test_keyword_eval_search_uses_keyword_retriever(monkeypatch) -> None:
    settings = Settings(
        database_url="postgresql://example",
        knowledge_root=Path("knowledge"),
        embedding_model="model",
        embedding_dimensions=384,
    )
    calls: list[tuple[str, int, str, bool]] = []

    def fake_keyword_search(
        passed_settings: Settings,
        query: str,
        limit: int,
        text_config: str,
        verbose: bool,
    ) -> list[SearchResult]:
        assert passed_settings is settings
        calls.append((query, limit, text_config, verbose))
        return [_result("keyword.md", "Keyword", 0.5)]

    monkeypatch.setattr("knowledge_system.evaluation.keyword_search", fake_keyword_search)

    search = build_keyword_search(settings, text_config="simple", verbose=False)

    assert search("Zinsen", 5)[0].source_path == "keyword.md"
    assert calls == [("Zinsen", 5, "simple", False)]


def test_build_search_defaults_to_vector(monkeypatch) -> None:
    settings = Settings(
        database_url="postgresql://example",
        knowledge_root=Path("knowledge"),
        embedding_model="model",
        embedding_dimensions=384,
    )
    calls: list[str] = []

    def fake_vector_search(passed_settings: Settings, verbose: bool):
        assert passed_settings is settings
        assert verbose is False
        calls.append("vector")
        return lambda query, limit: [_result("vector.md", "Vector", 0.9)]

    monkeypatch.setattr("knowledge_system.evaluation.build_reusable_embedder_search", fake_vector_search)

    assert build_search(settings, verbose=False)("query", 5)[0].source_path == "vector.md"
    assert calls == ["vector"]


def test_build_search_selects_hybrid(monkeypatch) -> None:
    settings = Settings(
        database_url="postgresql://example",
        knowledge_root=Path("knowledge"),
        embedding_model="model",
        embedding_dimensions=384,
    )
    calls: list[str] = []

    def fake_hybrid_search(passed_settings: Settings, verbose: bool):
        assert passed_settings is settings
        assert verbose is False
        calls.append("hybrid")
        return lambda query, limit: [_result("hybrid.md", "Hybrid", 0.9)]

    monkeypatch.setattr("knowledge_system.evaluation.build_hybrid_search", fake_hybrid_search)

    assert build_search(settings, retriever="hybrid", verbose=False)("query", 5)[0].source_path == "hybrid.md"
    assert calls == ["hybrid"]


def test_hybrid_eval_search_fuses_top_20_vector_and_keyword_candidates() -> None:
    settings = Settings(
        database_url="postgresql://example",
        knowledge_root=Path("knowledge"),
        embedding_model="model",
        embedding_dimensions=384,
    )
    calls: list[tuple[str, str, int]] = []

    def vector_search(query: str, limit: int) -> list[SearchResult]:
        calls.append(("vector", query, limit))
        return [
            _result("vector-only.md", "Vector", 0.9, chunk_key="chunk-vector-only"),
            _result("shared.md", "Shared", 0.8, chunk_key="chunk-shared"),
        ]

    def keyword_search_fn(query: str, limit: int) -> list[SearchResult]:
        calls.append(("keyword", query, limit))
        return [
            _result("shared.md", "Shared", 0.4, chunk_key="chunk-shared"),
            _result("keyword-only.md", "Keyword", 0.3, chunk_key="chunk-keyword-only"),
        ]

    search = build_hybrid_search(
        settings,
        verbose=False,
        vector_search=vector_search,
        keyword_search_fn=keyword_search_fn,
    )

    results = search("query", 5)

    assert calls == [("vector", "query", 20), ("keyword", "query", 20)]
    assert [result.chunk_key for result in results] == [
        "chunk-shared",
        "chunk-vector-only",
        "chunk-keyword-only",
    ]


def test_run_retrieval_eval_selects_keyword(monkeypatch, tmp_path: Path) -> None:
    suite = tmp_path / "suite.jsonl"
    suite.write_text(
        '{"id":"C-1","query":"query","expected_sources":["keyword.md"]}',
        encoding="utf-8",
    )
    settings = Settings(
        database_url="postgresql://example",
        knowledge_root=Path("knowledge"),
        embedding_model="model",
        embedding_dimensions=384,
    )

    def fake_build_search(
        passed_settings: Settings,
        retriever: str,
        text_config: str,
        verbose: bool,
    ):
        assert passed_settings is settings
        assert retriever == "keyword"
        assert text_config == "simple"
        assert verbose is False
        return lambda query, limit: [_result("keyword.md", "Keyword", 0.5)]

    monkeypatch.setattr("knowledge_system.evaluation.build_search", fake_build_search)

    report = run_retrieval_eval(
        settings,
        suite,
        retriever="keyword",
        text_config="simple",
        verbose=False,
    )

    assert report.retriever == "keyword"
    assert report.text_config == "simple"
    assert report.hit_rate_at(1) == 1.0
    assert report.to_dict()["retriever"] == "keyword"


def test_run_retrieval_eval_selects_hybrid_with_german_keyword(monkeypatch, tmp_path: Path) -> None:
    suite = tmp_path / "suite.jsonl"
    suite.write_text(
        '{"id":"C-1","query":"query","expected_sources":["hybrid.md"]}',
        encoding="utf-8",
    )
    settings = Settings(
        database_url="postgresql://example",
        knowledge_root=Path("knowledge"),
        embedding_model="model",
        embedding_dimensions=384,
    )

    def fake_build_search(
        passed_settings: Settings,
        retriever: str,
        text_config: str,
        verbose: bool,
    ):
        assert passed_settings is settings
        assert retriever == "hybrid"
        assert text_config == "german"
        assert verbose is False
        return lambda query, limit: [_result("hybrid.md", "Hybrid", 0.5)]

    monkeypatch.setattr("knowledge_system.evaluation.build_search", fake_build_search)

    report = run_retrieval_eval(
        settings,
        suite,
        retriever="hybrid",
        text_config="simple",
        verbose=False,
    )

    assert report.retriever == "hybrid"
    assert report.text_config == "german"
    assert report.hit_rate_at(1) == 1.0
    assert report.to_dict()["retriever"] == "hybrid"
