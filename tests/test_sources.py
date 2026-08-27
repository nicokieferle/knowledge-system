from __future__ import annotations

from pathlib import Path

from knowledge_system.sources import GitMarkdownSource, SourceDocument


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
