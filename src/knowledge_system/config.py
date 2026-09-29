from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
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
    http_client_id: str = ""
    http_bearer_token: str = field(default="", repr=False)


@dataclass(frozen=True)
class ChatClientSettings:
    llm_provider: str
    llm_model: str
    llm_api_key: str
    llm_base_url: str
    llm_timeout_seconds: float
    telegram_bot_token: str


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


def validate_apply_knowledge_root(path: Path, *, production: bool) -> None:
    """Fail closed before enabling browser writes to the configured data root."""
    try:
        raw = path.absolute()
        info = os.lstat(raw)
        resolved = raw.resolve(strict=True)
    except OSError:
        raise ValueError("KNOWLEDGE_ROOT must be an existing directory") from None
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or resolved != raw:
        raise ValueError("KNOWLEDGE_ROOT must be a real directory, not an alias")
    if not os.access(resolved, os.R_OK | os.W_OK | os.X_OK):
        raise ValueError("KNOWLEDGE_ROOT must be readable and writable by the review service")
    if production:
        if resolved == Path("/app") or Path("/app") in resolved.parents:
            raise ValueError("Production KNOWLEDGE_ROOT must be outside the application image")
        if info.st_uid != os.geteuid():
            raise ValueError("Production KNOWLEDGE_ROOT must be owned by the service UID")
        if stat.S_IMODE(info.st_mode) & 0o022:
            raise ValueError("Production KNOWLEDGE_ROOT must not be group/world writable")


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

    http_client_id = os.getenv("MCP_HTTP_CLIENT_ID", "").strip()
    http_bearer_token = os.getenv("MCP_HTTP_BEARER_TOKEN", "")
    if transport == "streamable-http":
        if not http_client_id or not http_bearer_token or len(http_bearer_token) < 32:
            raise ValueError(
                "MCP_HTTP_CLIENT_ID and a bearer token of at least 32 characters are required"
            )
        if not http_client_id.isascii() or not all(
            char.isalnum() or char in "_-" for char in http_client_id
        ):
            raise ValueError("MCP_HTTP_CLIENT_ID must contain only ASCII letters, digits, _ or -")
        if not http_bearer_token.isascii() or any(
            char.isspace() or not char.isprintable() for char in http_bearer_token
        ):
            raise ValueError("MCP_HTTP_BEARER_TOKEN must be printable ASCII without whitespace")

    return MCPServerSettings(
        transport=cast(MCPTransport, transport),
        host=host,
        port=port,
        path=path,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        http_client_id=http_client_id,
        http_bearer_token=http_bearer_token,
    )


def get_chat_client_settings() -> ChatClientSettings:
    _load_dotenv()
    provider = os.getenv("LLM_PROVIDER", "openai-compatible").strip()
    if provider != "openai-compatible":
        raise ValueError("Unsupported LLM_PROVIDER")
    settings = ChatClientSettings(
        llm_provider=provider,
        llm_model=os.getenv("LLM_MODEL", "").strip(),
        llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
        llm_base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").strip(),
        llm_timeout_seconds=float(os.getenv("LLM_TIMEOUT_SECONDS", "30")),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
    )
    if not settings.llm_model or not settings.llm_api_key or not settings.telegram_bot_token:
        raise ValueError("LLM_MODEL, LLM_API_KEY and TELEGRAM_BOT_TOKEN are required")
    return settings


def _parse_mcp_allowlist(name: str) -> tuple[str, ...]:
    values = tuple(value.strip() for value in os.getenv(name, "").split(",") if value.strip())
    if any("*" in value for value in values):
        raise ValueError(f"{name} must not contain wildcards")
    return values
