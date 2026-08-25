from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .search import SearchResult, semantic_search_with_embedder


@dataclass(frozen=True)
class EvalCase:
    id: str
    query: str
    expected_sources: tuple[str, ...]
    expected_headings: tuple[str, ...] = ()
    description: str = ""


@dataclass(frozen=True)
class EvalCaseResult:
    case: EvalCase
    results: tuple[SearchResult, ...]
    first_relevant_rank: int | None

    @property
    def reciprocal_rank(self) -> float:
        if self.first_relevant_rank is None:
            return 0.0
        return 1.0 / self.first_relevant_rank

    def hit_at(self, limit: int) -> bool:
        return self.first_relevant_rank is not None and self.first_relevant_rank <= limit


@dataclass(frozen=True)
class EvalReport:
    suite_path: Path
    limit: int
    case_results: tuple[EvalCaseResult, ...]

    @property
    def total(self) -> int:
        return len(self.case_results)

    def hit_rate_at(self, limit: int) -> float:
        return sum(result.hit_at(limit) for result in self.case_results) / self.total

    @property
    def mrr(self) -> float:
        return sum(result.reciprocal_rank for result in self.case_results) / self.total

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite_path": str(self.suite_path),
            "limit": self.limit,
            "total": self.total,
            "metrics": {
                "hit_at_1": self.hit_rate_at(1),
                "hit_at_3": self.hit_rate_at(3),
                "hit_at_5": self.hit_rate_at(5),
                "mrr": self.mrr,
            },
            "cases": [
                {
                    "id": result.case.id,
                    "query": result.case.query,
                    "expected_sources": list(result.case.expected_sources),
                    "expected_headings": list(result.case.expected_headings),
                    "description": result.case.description,
                    "first_relevant_rank": result.first_relevant_rank,
                    "reciprocal_rank": result.reciprocal_rank,
                    "results": [
                        {
                            "rank": rank,
                            "source_path": search_result.source_path,
                            "heading_path": search_result.heading_path,
                            "similarity": search_result.similarity,
                        }
                        for rank, search_result in enumerate(result.results, start=1)
                    ],
                }
                for result in self.case_results
            ],
        }


SearchFn = Callable[[str, int], list[SearchResult]]
SearchWithEmbedderFn = Callable[[Settings, str, object, int, bool], list[SearchResult]]
EmbedderFactory = Callable[[str, int, bool], object]


def _string_list(value: object, field_name: str, line_number: int) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Line {line_number}: `{field_name}` must be a list of strings")
    return tuple(value)


def _parse_eval_case(raw: object, line_number: int) -> EvalCase:
    if not isinstance(raw, dict):
        raise TypeError(f"Line {line_number}: eval case must be a JSON object")

    case_id = raw.get("id")
    query = raw.get("query")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError(f"Line {line_number}: `id` must be a non-empty string")
    if not isinstance(query, str) or not query:
        raise ValueError(f"Line {line_number}: `query` must be a non-empty string")

    expected_sources = _string_list(raw.get("expected_sources"), "expected_sources", line_number)
    if not expected_sources:
        raise ValueError(f"Line {line_number}: `expected_sources` must not be empty")

    expected_headings = _string_list(raw.get("expected_headings", []), "expected_headings", line_number)
    description = raw.get("description", "")
    if not isinstance(description, str):
        raise TypeError(f"Line {line_number}: `description` must be a string")

    return EvalCase(
        id=case_id,
        query=query,
        expected_sources=expected_sources,
        expected_headings=expected_headings,
        description=description,
    )


def load_eval_suite(path: Path) -> list[EvalCase]:
    cases: list[EvalCase] = []
    seen_ids: set[str] = set()

    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Line {line_number}: invalid JSON: {exc.msg}") from exc

        case = _parse_eval_case(raw, line_number)
        if case.id in seen_ids:
            raise ValueError(f"Line {line_number}: duplicate eval case id `{case.id}`")
        seen_ids.add(case.id)
        cases.append(case)

    if not cases:
        raise ValueError(f"No eval cases found in {path}")
    return cases


def _matches_expected(case: EvalCase, result: SearchResult) -> bool:
    if result.source_path not in case.expected_sources:
        return False
    return not case.expected_headings or result.heading_path in case.expected_headings


def evaluate_cases(cases: list[EvalCase], search: SearchFn, limit: int = 5) -> tuple[EvalCaseResult, ...]:
    if limit < 5:
        raise ValueError("Eval search limit must be at least 5 to compute Hit@5")
    if not cases:
        raise ValueError("Eval suite must contain at least one case")

    case_results: list[EvalCaseResult] = []
    for case in cases:
        results = tuple(search(case.query, limit))
        first_relevant_rank = next(
            (
                rank
                for rank, result in enumerate(results, start=1)
                if _matches_expected(case, result)
            ),
            None,
        )
        case_results.append(
            EvalCaseResult(
                case=case,
                results=results,
                first_relevant_rank=first_relevant_rank,
            )
        )

    return tuple(case_results)


def run_retrieval_eval(
    settings: Settings,
    suite_path: Path,
    limit: int = 5,
    verbose: bool = True,
) -> EvalReport:
    cases = load_eval_suite(suite_path)
    search = build_reusable_embedder_search(settings, verbose=verbose)

    return EvalReport(
        suite_path=suite_path,
        limit=limit,
        case_results=evaluate_cases(cases, search, limit=limit),
    )


def build_reusable_embedder_search(
    settings: Settings,
    verbose: bool = True,
    search_with_embedder: SearchWithEmbedderFn = semantic_search_with_embedder,
    embedder_factory: EmbedderFactory | None = None,
) -> SearchFn:
    if embedder_factory is None:
        from .embedder import LocalEmbedder

        embedder_factory = LocalEmbedder

    embedder = embedder_factory(settings.embedding_model, settings.embedding_dimensions, verbose)

    def search(query: str, search_limit: int) -> list[SearchResult]:
        return search_with_embedder(settings, query, embedder, search_limit, verbose)

    return search


def format_human_report(report: EvalReport) -> str:
    lines = [
        f"[eval] suite={report.suite_path}",
        f"[eval] cases={report.total} limit={report.limit}",
        "",
        "[eval] Summary",
        f"  Hit@1: {report.hit_rate_at(1):.3f}",
        f"  Hit@3: {report.hit_rate_at(3):.3f}",
        f"  Hit@5: {report.hit_rate_at(5):.3f}",
        f"  MRR:   {report.mrr:.3f}",
        "",
        "[eval] Cases",
    ]

    for result in report.case_results:
        status = "PASS" if result.first_relevant_rank is not None else "FAIL"
        rank = result.first_relevant_rank if result.first_relevant_rank is not None else "miss"
        lines.extend(
            [
                f"  [{status}] {result.case.id} rank={rank} rr={result.reciprocal_rank:.3f}",
                f"    query: {result.case.query}",
                f"    expected_sources: {', '.join(result.case.expected_sources)}",
            ]
        )
        if result.case.expected_headings:
            lines.append(f"    expected_headings: {', '.join(result.case.expected_headings)}")
        if result.case.description:
            lines.append(f"    description: {result.case.description}")
        lines.append("    results:")
        for rank, search_result in enumerate(result.results, start=1):
            lines.append(
                "      "
                f"{rank}. similarity={search_result.similarity:.4f} "
                f"path={search_result.source_path} heading={search_result.heading_path}"
            )
        if not result.results:
            lines.append("      <none>")

    return "\n".join(lines)
