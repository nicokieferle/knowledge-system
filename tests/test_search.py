from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any

from knowledge_system.config import Settings
from knowledge_system.search import keyword_search


class FakeRows:
    def __init__(self, rows: list[tuple[str, str, str, float]]) -> None:
        self.rows = rows

    def fetchall(self) -> list[tuple[str, str, str, float]]:
        return self.rows


class FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, params: tuple[Any, ...]) -> FakeRows:
        self.calls.append((sql, params))
        return FakeRows(
            [
                (
                    "economics/inflation.md",
                    "Inflation > Angebotsschocks",
                    "Inflation > Angebotsschocks\n\nEngpässe und Energiepreise.",
                    0.42,
                )
            ]
        )


def _settings() -> Settings:
    return Settings(
        database_url="postgresql://example",
        knowledge_root=Path("knowledge"),
        embedding_model="model",
        embedding_dimensions=384,
    )


def test_keyword_search_returns_ranked_results_for_german_terms(monkeypatch) -> None:
    fake_conn = FakeConnection()

    @contextmanager
    def fake_connect(settings: Settings):
        yield fake_conn

    monkeypatch.setattr("knowledge_system.search.connect", fake_connect)

    results = keyword_search(
        _settings(),
        "Welche Rolle spielen Engpässe und Energiepreise für Inflation?",
        text_config="german",
        verbose=False,
    )

    assert results[0].source_path == "economics/inflation.md"
    assert results[0].heading_path == "Inflation > Angebotsschocks"
    assert results[0].similarity == 0.42
    assert fake_conn.calls[0][1][:4] == (
        "german",
        "Welche Rolle spielen Engpässe und Energiepreise für Inflation?",
        "german",
        "german",
    )


def test_keyword_search_handles_empty_query_without_sql(monkeypatch) -> None:
    fake_conn = FakeConnection()

    @contextmanager
    def fake_connect(settings: Settings):
        yield fake_conn

    monkeypatch.setattr("knowledge_system.search.connect", fake_connect)

    assert keyword_search(_settings(), "   ", verbose=False) == []
    assert fake_conn.calls == []


def test_keyword_search_accepts_unusual_keyword_query(monkeypatch) -> None:
    fake_conn = FakeConnection()

    @contextmanager
    def fake_connect(settings: Settings):
        yield fake_conn

    monkeypatch.setattr("knowledge_system.search.connect", fake_connect)

    results = keyword_search(_settings(), '"Realzins" OR +Inflation -Assetpreise', verbose=False)

    assert len(results) == 1
    assert fake_conn.calls[0][1][1] == '"Realzins" OR +Inflation -Assetpreise'
