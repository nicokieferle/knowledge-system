from __future__ import annotations

from pathlib import Path

from knowledge_system.indexer import discover_markdown


def test_discover_markdown_excludes_root_readme(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()
    (root / "README.md").write_text("# Metadata\n\nNot canonical knowledge.", encoding="utf-8")
    topic = root / "economics"
    topic.mkdir()
    thesis = topic / "fiscal-dominance.md"
    thesis.write_text("# Fiskalische Dominanz\n\nThese.", encoding="utf-8")

    files = discover_markdown(root)

    assert files == [thesis]
