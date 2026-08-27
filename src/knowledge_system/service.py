from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Literal, Protocol

import psycopg

from .config import Settings
from .indexer import index_knowledge
from .reranker import LocalReranker
from .search import SearchResult, keyword_search
from .sources import GitMarkdownSource, SourceAdapter, SourceDocument

RetrievalMode = Literal["fast", "quality"]
QUALITY_CANDIDATE_LIMIT = 10


class KnowledgeServiceError(Exception):
    """Base class for failures exposed through the service boundary."""


class KnowledgeIndexUnavailableError(KnowledgeServiceError):
    """Raised when the disposable retrieval index cannot be queried."""


class UnknownSourceError(KnowledgeServiceError):
    """Raised when a caller requests an unregistered source adapter."""


class InvalidSourcePathError(KnowledgeServiceError):
    """Raised when a logical source path is invalid for its adapter."""


class SourceDocumentNotFoundError(KnowledgeServiceError):
    """Raised when a source adapter cannot find a requested document."""


class SourceAccessError(KnowledgeServiceError):
    """Raised when a registered source cannot be read."""


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
        self._reranker_lock = Lock()
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
        if mode not in ("fast", "quality"):
            raise ValueError(f"Unknown retrieval mode `{mode}`. Allowed: fast, quality")

        try:
            if mode == "fast":
                results = keyword_search(
                    self.settings,
                    query,
                    limit=limit,
                    text_config="german",
                    verbose=self.verbose,
                )
                return [self._to_service_result(result, mode) for result in results]

            candidates = keyword_search(
                self.settings,
                query,
                limit=max(QUALITY_CANDIDATE_LIMIT, limit),
                text_config="german",
                verbose=self.verbose,
            )
            results = self._get_reranker().rerank(query, candidates, limit=limit)
            return [self._to_service_result(result, mode) for result in results]
        except psycopg.Error as exc:
            raise KnowledgeIndexUnavailableError("Knowledge index is unavailable") from exc

    def get_document(
        self,
        source_path: str,
        source_id: str | None = None,
    ) -> SourceDocument:
        resolved_source_id = source_id or self.sources[0].source_id
        try:
            source = self._sources_by_id[resolved_source_id]
        except KeyError as exc:
            raise UnknownSourceError(f"Unknown source_id `{resolved_source_id}`") from exc

        try:
            return source.get_document(source_path)
        except FileNotFoundError as exc:
            raise SourceDocumentNotFoundError(
                f"Unknown source_path `{source_path}` for source_id `{resolved_source_id}`"
            ) from exc
        except ValueError as exc:
            raise InvalidSourcePathError(
                f"Invalid source_path `{source_path}` for source_id `{resolved_source_id}`"
            ) from exc
        except OSError as exc:
            raise SourceAccessError(
                f"Source document is unavailable for source_id `{resolved_source_id}`"
            ) from exc

    def _get_reranker(self) -> Reranker:
        if self._reranker is None:
            with self._reranker_lock:
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
