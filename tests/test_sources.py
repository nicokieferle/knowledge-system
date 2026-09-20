from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from knowledge_system.index_coordination import (
    GLOBAL_INDEX_LOCK_KEY,
    document_lock_key,
    parent_write_lock_key,
)
from knowledge_system.proposal_review import InvalidTarget
from knowledge_system.sources import (
    GitMarkdownSource,
    SourceDocument,
    portable_source_lock_identity,
)


def test_git_markdown_source_discovers_current_knowledge_documents() -> None:
    documents = GitMarkdownSource(Path("knowledge")).discover()

    assert len(documents) == 15
    assert "README.md" not in {document.source_path for document in documents}


def test_git_markdown_source_excludes_root_readme_and_uses_relative_paths(
    tmp_path: Path,
) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()
    (root / "README.md").write_text("# Metadata\n\nNot canonical knowledge.", encoding="utf-8")
    economics = root / "economics"
    economics.mkdir()
    (economics / "inflation.md").write_text("# Inflation\n\nContent.", encoding="utf-8")

    documents = GitMarkdownSource(root, source_id="economics-git").discover()

    assert [document.source_path for document in documents] == ["economics/inflation.md"]
    assert documents[0].source_id == "economics-git"
    assert not Path(documents[0].source_path).is_absolute()
    assert "\\" not in documents[0].source_path


def test_source_document_model() -> None:
    document = SourceDocument(
        source_id="knowledge-git",
        source_path="economics/inflation.md",
        content="# Inflation\n\nContent.",
        metadata={"path": "economics/inflation.md"},
    )

    assert document.source_id == "knowledge-git"
    assert document.source_path == "economics/inflation.md"
    assert "Inflation" in document.content
    assert document.metadata["path"] == "economics/inflation.md"


def test_git_markdown_source_get_document_reads_original_source(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()
    economics = root / "economics"
    economics.mkdir()
    path = economics / "inflation.md"
    path.write_text("---\nid: inflation\n---\n\n# Inflation\n\nOriginal.", encoding="utf-8")

    document = GitMarkdownSource(root).get_document("economics/inflation.md")

    assert document.content.startswith("---")
    assert "Original." in document.content


def test_portable_lock_identity_uses_the_validated_case_namespace() -> None:
    assert portable_source_lock_identity("knowledge-git", "Folder/Note.md") == (
        portable_source_lock_identity("knowledge-git", "folder/note.md")
    )
    with pytest.raises(InvalidTarget):
        portable_source_lock_identity("knowledge-git", "nöté.md")


def test_document_lock_keys_are_stable_casefolded_and_domain_separated() -> None:
    first = document_lock_key("knowledge-git", "p3059.md")
    former_collision = document_lock_key("knowledge-git", "p98538.md")
    alias = document_lock_key("knowledge-git", "Folder/Note.md")

    assert first != former_collision
    assert alias == document_lock_key("knowledge-git", "folder/note.md")
    assert first < 0 and former_collision < 0 and alias < 0
    assert GLOBAL_INDEX_LOCK_KEY > 0
    output = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from knowledge_system.index_coordination import document_lock_key; "
                "print(document_lock_key('knowledge-git', 'p3059.md'))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert int(output) == first


def test_parent_write_keys_are_casefolded_scoped_and_domain_separated():
    root = parent_write_lock_key("knowledge-git", "a.md")
    assert root == parent_write_lock_key("knowledge-git", "b.md")
    folder = parent_write_lock_key("knowledge-git", "Folder/a.md")
    assert folder == parent_write_lock_key("knowledge-git", "folder/B.md")
    assert folder != root
    assert -(1 << 62) <= root < -(1 << 61)
    assert document_lock_key("knowledge-git", "a.md") < -(1 << 62)
    assert GLOBAL_INDEX_LOCK_KEY > 0
    output = subprocess.run(
        [
            sys.executable,
            "-c",
            "from knowledge_system.index_coordination import parent_write_lock_key; print(parent_write_lock_key('knowledge-git', 'a.md'))",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert int(output) == root
