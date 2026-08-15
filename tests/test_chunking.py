from pathlib import Path

from knowledge_system.chunking import chunk_markdown


def test_chunk_markdown_uses_heading_context(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()
    path = root / "test.md"
    path.write_text(
        "# Geldsystem\n\nEinleitung.\n\n## Inflation\n\nGeldmenge ist nicht identisch mit CPI.",
        encoding="utf-8",
    )

    chunks = chunk_markdown(path, root)

    assert len(chunks) == 2
    assert chunks[0].heading_path == "Geldsystem"
    assert chunks[1].heading_path == "Geldsystem > Inflation"
    assert "Geldmenge" in chunks[1].content


def test_chunk_keys_are_deterministic(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()
    path = root / "test.md"
    path.write_text("# A\n\nText.", encoding="utf-8")

    first = chunk_markdown(path, root)
    second = chunk_markdown(path, root)

    assert first[0].chunk_key == second[0].chunk_key
    assert first[0].content_hash == second[0].content_hash
