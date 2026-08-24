from __future__ import annotations

from pathlib import Path

import pytest

from knowledge_system.evaluation import EvalCase, EvalReport, evaluate_cases, format_human_report
from knowledge_system.search import SearchResult


def _result(source_path: str, heading_path: str, similarity: float) -> SearchResult:
    return SearchResult(
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
