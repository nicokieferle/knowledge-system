from __future__ import annotations

import pytest

from knowledge_system.reranker import LocalReranker
from knowledge_system.search import SearchResult


class FakeCrossEncoder:
    def __init__(self, scores: list[float]) -> None:
        self.scores = scores
        self.calls: list[list[tuple[str, str]]] = []

    def predict(self, sentences: list[tuple[str, str]], **kwargs: object) -> list[float]:
        assert kwargs["convert_to_numpy"] is True
        self.calls.append(sentences)
        return self.scores


def test_reranker_receives_query_and_candidate_text() -> None:
    model = FakeCrossEncoder([0.7])
    reranker = LocalReranker(verbose=False, cross_encoder=model)
    candidate = _candidate(
        chunk_key="chunk-a",
        source_path="economics/a.md",
        heading_path="A > One",
        content="candidate content",
        similarity=0.1,
    )

    reranker.rerank("query text", [candidate], limit=5)

    assert model.calls == [[("query text", "A > One\n\ncandidate content")]]


def test_reranker_scores_reorder_candidates() -> None:
    model = FakeCrossEncoder([0.1, 0.9])
    reranker = LocalReranker(verbose=False, cross_encoder=model)
    low = _candidate("chunk-low", "economics/low.md", "Low", "low content", 10.0)
    high = _candidate("chunk-high", "economics/high.md", "High", "high content", 0.1)

    results = reranker.rerank("query", [low, high], limit=5)

    assert [result.chunk_key for result in results] == ["chunk-high", "chunk-low"]
    assert [result.similarity for result in results] == pytest.approx([0.9, 0.1])


def test_reranker_preserves_chunk_metadata() -> None:
    model = FakeCrossEncoder([1.0])
    reranker = LocalReranker(verbose=False, cross_encoder=model)
    candidate = _candidate(
        chunk_key="chunk-a",
        source_path="economics/a.md",
        heading_path="A > One",
        content="candidate content",
        similarity=0.1,
    )

    result = reranker.rerank("query", [candidate], limit=5)[0]

    assert result.chunk_key == candidate.chunk_key
    assert result.source_id == candidate.source_id
    assert result.source_path == candidate.source_path
    assert result.heading_path == candidate.heading_path
    assert result.content == candidate.content


def _candidate(
    chunk_key: str,
    source_path: str,
    heading_path: str,
    content: str,
    similarity: float,
) -> SearchResult:
    return SearchResult(
        chunk_key=chunk_key,
        source_id="knowledge-git",
        source_path=source_path,
        heading_path=heading_path,
        content=content,
        similarity=similarity,
    )
