from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL is required for the real PostgreSQL integration smoke",
)
def test_conversation_postgres_smoke() -> None:
    env = os.environ.copy()
    env["V30_SMOKE_CONFIRM"] = "isolated-v30-smoke-database"
    completed = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "conversation_postgres_smoke.py")],
        cwd=PROJECT_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "schema_initialized=true" in completed.stdout
    assert "confirmation_atomic=true" in completed.stdout
    assert "duplicate_confirmation_idempotent=true" in completed.stdout
    assert "concurrent_confirmation_tested=true" in completed.stdout
    assert "knowledge_unchanged=true" in completed.stdout
