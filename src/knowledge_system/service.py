from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from .config import Settings
from .indexer import index_knowledge
from .reranker import LocalReranker
from .search import SearchResult, keyword_search
from .sources import GitMarkdownSource, SourceAdapter, SourceDocument

RetrievalMode = Literal["fast", "quality"]
QUALITY_CANDIDATE_LIMIT = 10


class Reranker(Protocol):
    def rerank(
        self,
        query: str,
        candidates: list[SearchResult],
        limit: int = 5,
    ) -> list[SearchResult]: ...


@dataclass(frozen=True)
class KnowledgeSearchResult:
    chunk_key: str
    source_id: str
    source_path: str
    heading_path: str
    content: str
    score: float
    retrieval_mode: RetrievalMode


class KnowledgeService:
    def __init__(
        self,
        settings: Settings,
        sources: list[SourceAdapter] | None = None,
        reranker: Reranker | None = None,
        verbose: bool = True,
    ) -> None:
        self.settings = settings
        self.sources = sources or [GitMarkdownSource(settings.knowledge_root)]
        self.verbose = verbose
        self._reranker = reranker
        self._sources_by_id = {source.source_id: source for source in self.sources}
        if len(self._sources_by_id) != len(self.sources):
            raise ValueError("Source IDs must be unique")

    def index(self) -> None:
        for source in self.sources:
            index_knowledge(self.settings, source=source)

    def search(
        self,
        query: str,
        mode: RetrievalMode = "quality",
        limit: int = 5,
    ) -> list[KnowledgeSearchResult]:
        if mode == "fast":
            results = keyword_search(
                self.settings,
                query,
                limit=limit,
                text_config="german",
                verbose=self.verbose,
            )
            return [self._to_service_result(result, mode) for result in results]

        if mode == "quality":
            candidates = keyword_search(
                self.settings,
                query,
                limit=max(QUALITY_CANDIDATE_LIMIT, limit),
                text_config="german",
                verbose=self.verbose,
            )
            results = self._get_reranker().rerank(query, candidates, limit=limit)
            return [self._to_service_result(result, mode) for result in results]

        raise ValueError(f"Unknown retrieval mode `{mode}`. Allowed: fast, quality")

    def get_document(
        self,
        source_path: str,
        source_id: str | None = None,
    ) -> SourceDocument:
        resolved_source_id = source_id or self.sources[0].source_id
        try:
            source = self._sources_by_id[resolved_source_id]
        except KeyError as exc:
            raise ValueError(f"Unknown source_id `{resolved_source_id}`") from exc
        return source.get_document(source_path)

    def _get_reranker(self) -> Reranker:
        if self._reranker is None:
            self._reranker = LocalReranker(verbose=self.verbose)
        return self._reranker

    @staticmethod
    def _to_service_result(
        result: SearchResult,
        mode: RetrievalMode,
    ) -> KnowledgeSearchResult:
        return KnowledgeSearchResult(
            chunk_key=result.chunk_key,
            source_id=result.source_id,
            source_path=result.source_path,
            heading_path=result.heading_path,
            content=result.content,
            score=result.similarity,
            retrieval_mode=mode,
        )
