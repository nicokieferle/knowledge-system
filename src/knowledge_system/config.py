from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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


def get_settings() -> Settings:
    _load_dotenv()

    return Settings(
        database_url=os.getenv(
            "DATABASE_URL",
            "postgresql://knowledge:knowledge@localhost:5432/knowledge",
        ),
        knowledge_root=Path(os.getenv("KNOWLEDGE_ROOT", "./knowledge")).resolve(),
        embedding_model=os.getenv(
            "EMBEDDING_MODEL",
            "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        ),
        embedding_dimensions=int(os.getenv("EMBEDDING_DIMENSIONS", "384")),
    )
