from __future__ import annotations

from knowledge_system.config import _with_default_connect_timeout


def test_database_url_gets_default_connect_timeout() -> None:
    url = _with_default_connect_timeout("postgresql://user:pass@localhost:5432/db")

    assert url == "postgresql://user:pass@localhost:5432/db?connect_timeout=5"


def test_database_url_keeps_explicit_connect_timeout() -> None:
    url = _with_default_connect_timeout(
        "postgresql://user:pass@localhost:5432/db?connect_timeout=2"
    )

    assert url == "postgresql://user:pass@localhost:5432/db?connect_timeout=2"
