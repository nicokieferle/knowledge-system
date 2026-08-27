from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

ROOT_MARKDOWN_EXCLUDES = {"README.md"}
DEFAULT_GIT_SOURCE_ID = "knowledge-git"


@dataclass(frozen=True)
class SourceDocument:
    source_id: str
    source_path: str
    content: str
    metadata: dict[str, object] = field(default_factory=dict)


class SourceAdapter(Protocol):
    source_id: str

    def discover(self) -> list[SourceDocument]: ...

    def get_document(self, source_path: str) -> SourceDocument: ...


class GitMarkdownSource:
    def __init__(
        self,
        root: Path,
        source_id: str = DEFAULT_GIT_SOURCE_ID,
    ) -> None:
        self.root = root
        self.source_id = source_id

    def discover(self) -> list[SourceDocument]:
        if not self.root.exists():
            raise FileNotFoundError(f"Knowledge root does not exist: {self.root}")

        return [
            self._read_path(path)
            for path in sorted(self.root.rglob("*.md"))
            if self._is_indexable_path(path)
        ]

    def get_document(self, source_path: str) -> SourceDocument:
        path = self._resolve_source_path(source_path)
        if not self._is_indexable_path(path):
            raise FileNotFoundError(f"Source document is not indexable: {source_path}")
        return self._read_path(path)

    def _is_indexable_path(self, path: Path) -> bool:
        if not path.is_file():
            return False
        try:
            relative = path.relative_to(self.root)
        except ValueError:
            return False
        relative_posix = relative.as_posix()
        return (
            relative_posix not in ROOT_MARKDOWN_EXCLUDES
            and not any(part.startswith(".") for part in relative.parts)
        )

    def _read_path(self, path: Path) -> SourceDocument:
        relative_path = path.relative_to(self.root).as_posix()
        return SourceDocument(
            source_id=self.source_id,
            source_path=relative_path,
            content=path.read_text(encoding="utf-8"),
            metadata={"path": relative_path},
        )

    def _resolve_source_path(self, source_path: str) -> Path:
        if Path(source_path).is_absolute():
            raise ValueError("source_path must be relative")

        path = (self.root / source_path).resolve()
        root = self.root.resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"source_path escapes source root: {source_path}") from exc
        return path
