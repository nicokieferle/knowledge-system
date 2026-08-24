from __future__ import annotations

import subprocess
import sys


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
