from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from knowledge_system.config import Settings
from knowledge_system.evaluation import EvalCase, EvalCaseResult, EvalReport
from knowledge_system.search import SearchResult


def test_cli_module_import_does_not_load_embedding_stack() -> None:
    code = """
import sys
import knowledge_system.cli

for name in (
    "knowledge_system.indexer",
    "knowledge_system.search",
    "knowledge_system.embedder",
    "sentence_transformers",
    "torch",
):
    assert name not in sys.modules, name
"""

    subprocess.run([sys.executable, "-c", code], check=True)


def test_index_and_search_module_imports_do_not_load_embedding_stack() -> None:
    code = """
import sys
import knowledge_system.indexer
import knowledge_system.search

for name in (
    "knowledge_system.embedder",
    "sentence_transformers",
    "torch",
):
    assert name not in sys.modules, name
"""

    subprocess.run([sys.executable, "-c", code], check=True)


def test_eval_json_output_supports_vector_retriever(monkeypatch, capsys) -> None:
    from knowledge_system import cli

    def fake_get_settings() -> Settings:
        return Settings(
            database_url="postgresql://example",
            knowledge_root=Path("knowledge"),
            embedding_model="model",
            embedding_dimensions=384,
        )

    def fake_run_retrieval_eval(
        settings: Settings,
        suite_path: Path,
        limit: int,
        retriever: str,
        text_config: str,
        verbose: bool,
    ) -> EvalReport:
        assert retriever == "vector"
        assert text_config == "german"
        assert verbose is False
        return _report(retriever="vector")

    monkeypatch.setattr(cli, "get_settings", fake_get_settings)
    monkeypatch.setattr(sys, "argv", ["knowledge", "eval", "--json"])
    monkeypatch.setattr("knowledge_system.evaluation.run_retrieval_eval", fake_run_retrieval_eval)

    cli.main()

    output = json.loads(capsys.readouterr().out)
    assert output["retriever"] == "vector"
    assert output["text_config"] is None


def test_eval_json_output_supports_keyword_retriever(monkeypatch, capsys) -> None:
    from knowledge_system import cli

    def fake_get_settings() -> Settings:
        return Settings(
            database_url="postgresql://example",
            knowledge_root=Path("knowledge"),
            embedding_model="model",
            embedding_dimensions=384,
        )

    def fake_run_retrieval_eval(
        settings: Settings,
        suite_path: Path,
        limit: int,
        retriever: str,
        text_config: str,
        verbose: bool,
    ) -> EvalReport:
        assert retriever == "keyword"
        assert text_config == "simple"
        assert verbose is False
        return _report(retriever="keyword", text_config="simple")

    monkeypatch.setattr(cli, "get_settings", fake_get_settings)
    monkeypatch.setattr(
        sys,
        "argv",
        ["knowledge", "eval", "--json", "--retriever", "keyword", "--text-config", "simple"],
    )
    monkeypatch.setattr("knowledge_system.evaluation.run_retrieval_eval", fake_run_retrieval_eval)

    cli.main()

    output = json.loads(capsys.readouterr().out)
    assert output["retriever"] == "keyword"
    assert output["text_config"] == "simple"


def test_eval_json_output_supports_hybrid_retriever(monkeypatch, capsys) -> None:
    from knowledge_system import cli

    def fake_get_settings() -> Settings:
        return Settings(
            database_url="postgresql://example",
            knowledge_root=Path("knowledge"),
            embedding_model="model",
            embedding_dimensions=384,
        )

    def fake_run_retrieval_eval(
        settings: Settings,
        suite_path: Path,
        limit: int,
        retriever: str,
        text_config: str,
        verbose: bool,
    ) -> EvalReport:
        assert retriever == "hybrid"
        assert text_config == "german"
        assert verbose is False
        return _report(retriever="hybrid", text_config="german")

    monkeypatch.setattr(cli, "get_settings", fake_get_settings)
    monkeypatch.setattr(
        sys,
        "argv",
        ["knowledge", "eval", "--json", "--retriever", "hybrid"],
    )
    monkeypatch.setattr("knowledge_system.evaluation.run_retrieval_eval", fake_run_retrieval_eval)

    cli.main()

    output = json.loads(capsys.readouterr().out)
    assert output["retriever"] == "hybrid"
    assert output["text_config"] == "german"


def _report(retriever: str, text_config: str | None = None) -> EvalReport:
    case = EvalCase(id="C-1", query="query", expected_sources=("result.md",))
    result = SearchResult(
        source_path="result.md",
        heading_path="Heading",
        content="content",
        similarity=1.0,
    )
    return EvalReport(
        suite_path=Path("suite.jsonl"),
        limit=5,
        case_results=(EvalCaseResult(case=case, results=(result,), first_relevant_rank=1),),
        retriever=retriever,
        text_config=text_config,
    )
