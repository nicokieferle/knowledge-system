from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from knowledge_system.config import Settings
from knowledge_system.durable_state import read_durable_state_fingerprint


def main() -> int:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2

    settings = Settings(
        database_url=database_url,
        knowledge_root=Path(os.getenv("KNOWLEDGE_ROOT", "knowledge")),
        embedding_model=os.getenv("EMBEDDING_MODEL", "unused-for-fingerprint"),
        embedding_dimensions=int(os.getenv("EMBEDDING_DIMENSIONS", "384")),
    )
    try:
        fingerprint = read_durable_state_fingerprint(settings)
    except Exception as exc:  # noqa: BLE001 - redact all unexpected database failures.
        print(f"fingerprint_error={type(exc).__name__}", file=sys.stderr)
        return 1

    print(f"durable_state_counts={json.dumps(fingerprint.counts, sort_keys=True)}")
    print(f"durable_state_sha256={fingerprint.sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
