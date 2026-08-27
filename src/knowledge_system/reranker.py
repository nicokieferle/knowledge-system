from __future__ import annotations

import time
from typing import Protocol

import numpy as np

from .search import SearchResult

DEFAULT_RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"


class CrossEncoderLike(Protocol):
    def predict(self, sentences: list[tuple[str, str]], **kwargs: object) -> object: ...


class LocalReranker:
    def __init__(
        self,
        model_name: str = DEFAULT_RERANKER_MODEL,
        verbose: bool = True,
        cross_encoder: CrossEncoderLike | None = None,
    ) -> None:
        self.model_name = model_name
        self.verbose = verbose
        if cross_encoder is None:
            if self.verbose:
                print(f"[reranker] Loading model: {model_name}")
            start = time.perf_counter()
            from sentence_transformers import CrossEncoder

            cross_encoder = CrossEncoder(model_name)
            if self.verbose:
                elapsed = time.perf_counter() - start
                print(f"[reranker] Model ready: load_time={elapsed:.2f}s")
        self.model = cross_encoder

    def rerank(
        self,
        query: str,
        candidates: list[SearchResult],
        limit: int = 5,
    ) -> list[SearchResult]:
        if not candidates:
            if self.verbose:
                print("[reranker] No candidates to rerank")
            return []

        pairs = [(query, _candidate_text(candidate)) for candidate in candidates]
        if self.verbose:
            print(f"[reranker] Scoring {len(pairs)} candidate(s)")
        start = time.perf_counter()
        raw_scores = self.model.predict(pairs, convert_to_numpy=True)
        scores = np.asarray(raw_scores, dtype=np.float32).reshape(-1)
        if len(scores) != len(candidates):
            raise RuntimeError(
                f"Reranker returned {len(scores)} score(s) for {len(candidates)} candidate(s)"
            )
        if self.verbose:
            elapsed = time.perf_counter() - start
            print(f"[reranker] Scored {len(pairs)} candidate(s) in {elapsed:.2f}s")

        reranked = [
            SearchResult(
                chunk_key=candidate.chunk_key,
                source_path=candidate.source_path,
                heading_path=candidate.heading_path,
                content=candidate.content,
                similarity=float(score),
            )
            for candidate, score in zip(candidates, scores, strict=True)
        ]

        return sorted(
            reranked,
            key=lambda result: (
                -result.similarity,
                result.source_path,
                result.heading_path,
                result.chunk_key,
                result.content,
            ),
        )[:limit]


def _candidate_text(candidate: SearchResult) -> str:
    return f"{candidate.heading_path}\n\n{candidate.content}"
