from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from .config import MCPServerSettings, get_mcp_server_settings, get_settings
from .service import (
    InvalidSourcePathError,
    KnowledgeIndexUnavailableError,
    KnowledgeSearchResult,
    KnowledgeService,
    KnowledgeServiceError,
    SourceDocumentNotFoundError,
    UnknownSourceError,
)

LOGGER = logging.getLogger(__name__)
MIN_SEARCH_LIMIT = 1
MAX_SEARCH_LIMIT = 20
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True)
class SearchKnowledgeItem:
    chunk_key: str
    source_id: str
    source_path: str
    heading_path: str
    content: str
    score: float
    retrieval_mode: Literal["fast", "quality"]


@dataclass(frozen=True)
class SearchKnowledgeResponse:
    results: list[SearchKnowledgeItem]


@dataclass(frozen=True)
class GetDocumentResponse:
    source_id: str
    source_path: str
    content: str


def create_mcp_server(service: KnowledgeService | None = None) -> MCPServer:
    """Create one MCP server whose tools share one long-lived service instance."""
    knowledge_service = (
        service if service is not None else KnowledgeService(get_settings(), verbose=False)
    )
    server = MCPServer(
        "Knowledge System",
        instructions=(
            "Read-only access to the personal knowledge base. Search before answering "
            "questions that may be covered by internal knowledge, then fetch an original "
            "document when full context is needed."
        ),
    )

    @server.tool(
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    def search_knowledge(
        query: str,
        mode: Literal["fast", "quality"] = "quality",
        limit: int = 5,
    ) -> SearchKnowledgeResponse:
        """Search the personal knowledge base before answering from existing internal knowledge.

        Use `fast` for quick German keyword search. Use `quality` for German keyword
        candidates reranked by the local cross-encoder. Results are source-backed chunks;
        call `get_document` when a hit must be read completely or in its original context.
        """
        normalized_query = query.strip()
        if not normalized_query:
            raise ToolError("query must not be empty")
        if not MIN_SEARCH_LIMIT <= limit <= MAX_SEARCH_LIMIT:
            raise ToolError(
                f"limit must be between {MIN_SEARCH_LIMIT} and {MAX_SEARCH_LIMIT}"
            )

        try:
            results = knowledge_service.search(normalized_query, mode=mode, limit=limit)
        except KnowledgeIndexUnavailableError as exc:
            LOGGER.exception("Knowledge index unavailable during search")
            raise ToolError(
                "Knowledge index is unavailable. Verify that PostgreSQL is running and initialized."
            ) from exc
        except KnowledgeServiceError as exc:
            LOGGER.exception("Knowledge service failed during search")
            raise ToolError("Knowledge search is currently unavailable.") from exc
        except Exception as exc:
            LOGGER.exception("Unexpected knowledge search failure")
            raise ToolError("Knowledge search is currently unavailable.") from exc

        return SearchKnowledgeResponse(results=[_to_search_item(result) for result in results])

    @server.tool(
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    def get_document(source_id: str, source_path: str) -> GetDocumentResponse:
        """Fetch a canonical original document from a registered source adapter.

        Use this after `search_knowledge` when a search hit must be read completely or in
        its original context. `source_id` and `source_path` must come from a search result;
        arbitrary operating-system paths are not accepted.
        """
        try:
            document = knowledge_service.get_document(
                source_id=source_id,
                source_path=source_path,
            )
        except (UnknownSourceError, InvalidSourcePathError, SourceDocumentNotFoundError) as exc:
            raise ToolError(str(exc)) from exc
        except KnowledgeServiceError as exc:
            LOGGER.exception("Knowledge source unavailable during document read")
            raise ToolError("Knowledge source is currently unavailable.") from exc
        except Exception as exc:
            LOGGER.exception("Unexpected source document failure")
            raise ToolError("Knowledge document could not be read.") from exc

        return GetDocumentResponse(
            source_id=document.source_id,
            source_path=document.source_path,
            content=document.content,
        )

    return server


def _to_search_item(result: KnowledgeSearchResult) -> SearchKnowledgeItem:
    return SearchKnowledgeItem(
        chunk_key=result.chunk_key,
        source_id=result.source_id,
        source_path=result.source_path,
        heading_path=result.heading_path,
        content=result.content,
        score=result.score,
        retrieval_mode=result.retrieval_mode,
    )


def create_loopback_transport_security(
    host: str,
    port: int,
) -> TransportSecuritySettings:
    if host not in LOOPBACK_HOSTS:
        raise ValueError(
            "Non-loopback MCP_HOST requires explicit TransportSecuritySettings with an allowlist"
        )
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            f"127.0.0.1:{port}",
            f"localhost:{port}",
            f"[::1]:{port}",
        ],
        allowed_origins=[
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
            f"http://[::1]:{port}",
        ],
    )


def create_transport_security(
    settings: MCPServerSettings,
) -> TransportSecuritySettings:
    if not settings.allowed_hosts:
        return create_loopback_transport_security(settings.host, settings.port)

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(settings.allowed_hosts),
        allowed_origins=list(settings.allowed_origins),
    )


def run_mcp_server(
    settings: MCPServerSettings | None = None,
    service: KnowledgeService | None = None,
    transport_security: TransportSecuritySettings | None = None,
) -> None:
    resolved_settings = settings or get_mcp_server_settings()
    server = create_mcp_server(service)

    if resolved_settings.transport == "stdio":
        server.run(transport="stdio")
        return

    security = transport_security or create_transport_security(resolved_settings)
    server.run(
        transport="streamable-http",
        host=resolved_settings.host,
        port=resolved_settings.port,
        streamable_http_path=resolved_settings.path,
        transport_security=security,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    run_mcp_server()


if __name__ == "__main__":
    main()
