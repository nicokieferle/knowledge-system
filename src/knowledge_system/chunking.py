from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
FRONTMATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*(?:\n|\Z)", re.DOTALL)


@dataclass(frozen=True)
class Chunk:
    chunk_key: str
    source_path: str
    heading_path: str
    ordinal: int
    content: str
    content_hash: str


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _split_long_section(text: str, max_chars: int = 2800, overlap_chars: int = 250) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    chunks: list[str] = []
    current = ""

    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph

        if len(candidate) <= max_chars:
            current = candidate
            continue

        if current:
            chunks.append(current)

        # Extremely long paragraphs still need a deterministic fallback split.
        if len(paragraph) > max_chars:
            start = 0
            step = max(1, max_chars - overlap_chars)
            while start < len(paragraph):
                chunks.append(paragraph[start : start + max_chars].strip())
                start += step
            current = ""
        else:
            previous_tail = chunks[-1][-overlap_chars:] if chunks else ""
            current = f"{previous_tail}\n\n{paragraph}".strip()

    if current:
        chunks.append(current)

    return [chunk for chunk in chunks if chunk]


def _strip_frontmatter(text: str) -> str:
    return FRONTMATTER_RE.sub("", text, count=1)


def chunk_markdown(path: Path, root: Path) -> list[Chunk]:
    """Split Markdown primarily by semantic headings, then by paragraph size."""
    relative_path = path.relative_to(root).as_posix()
    return chunk_markdown_text(path.read_text(encoding="utf-8"), relative_path)


def chunk_markdown_text(text: str, source_path: str) -> list[Chunk]:
    """Split Markdown text while preserving the existing source-path based chunk keys."""
    text = _strip_frontmatter(text)

    heading_stack: list[tuple[int, str]] = []
    section_heading = "(document)"
    section_lines: list[str] = []
    sections: list[tuple[str, str]] = []

    def flush_section() -> None:
        nonlocal section_lines
        body = "\n".join(section_lines).strip()
        if body:
            sections.append((section_heading, body))
        section_lines = []

    for line in text.splitlines():
        match = HEADING_RE.match(line)
        if not match:
            section_lines.append(line)
            continue

        flush_section()

        level = len(match.group(1))
        title = match.group(2).strip()
        heading_stack[:] = [(lvl, name) for lvl, name in heading_stack if lvl < level]
        heading_stack.append((level, title))
        section_heading = " > ".join(name for _, name in heading_stack)

    flush_section()

    chunks: list[Chunk] = []
    ordinal = 0

    for heading_path, section_text in sections:
        for piece in _split_long_section(section_text):
            # Include heading context in the embedded content so short sections remain meaningful.
            content = f"{heading_path}\n\n{piece}".strip()
            key_basis = f"{source_path}|{heading_path}|{ordinal}"
            chunks.append(
                Chunk(
                    chunk_key=_hash_text(key_basis),
                    source_path=source_path,
                    heading_path=heading_path,
                    ordinal=ordinal,
                    content=content,
                    content_hash=_hash_text(content),
                )
            )
            ordinal += 1

    return chunks
