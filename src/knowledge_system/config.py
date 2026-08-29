from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MCPTransport = Literal["stdio", "streamable-http"]
MCP_TRANSPORTS = {"stdio", "streamable-http"}


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Tiny dotenv loader to avoid adding another runtime dependency."""
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    database_url: str
    knowledge_root: Path
    embedding_model: str
    embedding_dimensions: int


@dataclass(frozen=True)
class MCPServerSettings:
    transport: MCPTransport
    host: str
    port: int
    path: str
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()


def _with_default_connect_timeout(database_url: str, seconds: int = 5) -> str:
    if "connect_timeout" in database_url or not database_url.startswith(
        ("postgresql://", "postgres://")
    ):
        return database_url

    parts = urlsplit(database_url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.append(("connect_timeout", str(seconds)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def get_settings() -> Settings:
    _load_dotenv()

    return Settings(
        database_url=_with_default_connect_timeout(
            os.getenv(
                "DATABASE_URL",
                "postgresql://knowledge:knowledge@localhost:5432/knowledge",
            )
        ),
        knowledge_root=Path(os.getenv("KNOWLEDGE_ROOT", "./knowledge")).resolve(),
        embedding_model=os.getenv(
            "EMBEDDING_MODEL",
            "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        ),
        embedding_dimensions=int(os.getenv("EMBEDDING_DIMENSIONS", "384")),
    )


def get_mcp_server_settings() -> MCPServerSettings:
    _load_dotenv()

    transport = os.getenv("MCP_TRANSPORT", "stdio").strip().lower()
    if transport not in MCP_TRANSPORTS:
        allowed = ", ".join(sorted(MCP_TRANSPORTS))
        raise ValueError(f"Unsupported MCP_TRANSPORT `{transport}`. Allowed: {allowed}")

    host = os.getenv("MCP_HOST", "127.0.0.1").strip()
    if not host:
        raise ValueError("MCP_HOST must not be empty")

    raw_port = os.getenv("MCP_PORT", "8000").strip()
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError("MCP_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("MCP_PORT must be between 1 and 65535")

    path = os.getenv("MCP_PATH", "/mcp").strip()
    if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
        raise ValueError("MCP_PATH must be an absolute URL path such as `/mcp`")

    allowed_hosts = _parse_mcp_allowlist("MCP_ALLOWED_HOSTS")
    allowed_origins = _parse_mcp_allowlist("MCP_ALLOWED_ORIGINS")
    if allowed_origins and not allowed_hosts:
        raise ValueError("MCP_ALLOWED_ORIGINS requires MCP_ALLOWED_HOSTS")

    return MCPServerSettings(
        transport=cast(MCPTransport, transport),
        host=host,
        port=port,
        path=path,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


def _parse_mcp_allowlist(name: str) -> tuple[str, ...]:
    values = tuple(value.strip() for value in os.getenv(name, "").split(",") if value.strip())
    if any("*" in value for value in values):
        raise ValueError(f"{name} must not contain wildcards")
    return values
