from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

ROOT_MARKDOWN_EXCLUDES = {"README.md"}
DEFAULT_GIT_SOURCE_ID = "knowledge-git"


def validate_read_source_path(source_path: str) -> list[str]:
    """Validate the exact relative Markdown path emitted by discovery."""
    if not isinstance(source_path, str) or not source_path or "\x00" in source_path:
        raise ValueError("Invalid source path")
    parts = source_path.split("/")
    if (
        Path(source_path).is_absolute()
        or bool(Path(source_path).drive)
        or Path(source_path).as_posix() != source_path
        or source_path in ROOT_MARKDOWN_EXCLUDES
        or not source_path.endswith(".md")
        or any(not part or part in {".", ".."} or part.startswith(".") for part in parts)
    ):
        raise ValueError("Invalid source path")
    return parts


def validate_source_target(source_id: str, source_path: str) -> list[str]:
    """Validate the portable review/apply namespace and return its components."""
    from .proposal_review import InvalidTarget

    if source_id != DEFAULT_GIT_SOURCE_ID:
        raise InvalidTarget()
    if not isinstance(source_path, str) or not source_path or len(source_path) > 240:
        raise InvalidTarget()
    parts = source_path.split("/")
    if (
        source_path.casefold() in {name.casefold() for name in ROOT_MARKDOWN_EXCLUDES}
        or not source_path.endswith(".md")
        or any(
            not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_. -]*", part)
            or part.endswith((".", " "))
            or part.split(".")[0].upper()
            in {
                "CON",
                "PRN",
                "AUX",
                "NUL",
                *(f"COM{i}" for i in range(10)),
                *(f"LPT{i}" for i in range(10)),
            }
            for part in parts
        )
    ):
        raise InvalidTarget()
    return parts


def portable_source_lock_identity(source_id: str, source_path: str) -> str:
    """Return the case-insensitive identity used by portable collision locks.

    Validation deliberately defines the one path namespace used by review and
    apply. Its current portable alphabet is ASCII-only, so casefolding the
    validated components is deterministic without introducing another Unicode
    normalization policy.
    """

    canonical_source, canonical_path = portable_source_lock_components(source_id, source_path)
    return canonical_source + ":" + canonical_path


def portable_source_lock_components(source_id: str, source_path: str) -> tuple[str, str]:
    """Return the validated, case-insensitive source/path lock components."""

    parts = validate_source_target(source_id, source_path)
    return source_id, "/".join(part.casefold() for part in parts)


def _stable_file_metadata(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    """Return change-sensitive identity metadata while deliberately excluding atime."""

    return (
        value.st_dev,
        value.st_ino,
        stat.S_IFMT(value.st_mode),
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _reject_portable_case_alias(names: list[str], requested: str) -> None:
    """Reject a differently-cased existing component on every host filesystem."""

    from .proposal_review import InvalidTarget

    if any(name != requested and name.casefold() == requested.casefold() for name in names):
        raise InvalidTarget()


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
            self.get_document(path.relative_to(self.root).as_posix())
            for path in sorted(self.root.rglob("*.md"))
            if self._is_indexable_path(path)
        ]

    def get_document(self, source_path: str) -> SourceDocument:
        parts = validate_read_source_path(source_path)
        path = self.root.joinpath(*parts)
        if not self._is_indexable_path(path):
            raise FileNotFoundError(f"Source document is not indexable: {source_path}")
        if os.name == "posix":
            content = self._read_posix(parts)
        else:
            content = path.read_text(encoding="utf-8")
        return SourceDocument(
            source_id=self.source_id,
            source_path=source_path,
            content=content,
            metadata={"path": source_path},
        )

    def _is_indexable_path(self, path: Path) -> bool:
        if not path.is_file() or path.suffix != ".md":
            return False
        try:
            relative = path.relative_to(self.root)
        except ValueError:
            return False
        relative_posix = relative.as_posix()
        try:
            validate_read_source_path(relative_posix)
        except ValueError:
            return False
        current = self.root
        for part in relative.parts:
            current = current / part
            if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
                return False
        return True

    def _read_posix(self, parts: list[str]) -> str:
        """Read through pinned directories without following a symlink."""
        directory = os.open(self.root.resolve(strict=True), os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in parts[:-1]:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
                os.close(directory)
                directory = child
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise ValueError("Source document is not a regular file")
                return handle.read()
        finally:
            os.close(directory)

    def snapshot(self, source_id: str, source_path: str) -> tuple[str, str | None]:
        """Strict review read, including absent targets; never creates directories/files.

        Refuse symlinks (including in-root aliases), junctions and ambiguous portable
        paths. Knowledge mounts must not be writable by untrusted concurrent processes.
        V3.3 must perform its own atomic no-follow validation at apply time.
        """
        from .proposal_review import MAX_REVIEW_BYTES, InvalidTarget

        if self.source_id != DEFAULT_GIT_SOURCE_ID:
            raise InvalidTarget()
        parts = validate_source_target(source_id, source_path)
        try:
            root = self.root.resolve(strict=True)
            if os.name == "posix":
                return source_path, self._snapshot_posix(root, parts, MAX_REVIEW_BYTES)
            path = root
            for i, part in enumerate(parts):
                if not path.exists():
                    return source_path, None
                _reject_portable_case_alias([entry.name for entry in path.iterdir()], part)
                path = path / part
                if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                    raise InvalidTarget()
                if path.exists() and i < len(parts) - 1 and not path.is_dir():
                    raise InvalidTarget()
            if not path.resolve().is_relative_to(root):
                raise InvalidTarget()
            if not path.exists():
                return source_path, None
            before = path.stat()
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_REVIEW_BYTES:
                raise InvalidTarget()
            with path.open("rb") as handle:
                data = handle.read(MAX_REVIEW_BYTES + 1)
            after = path.stat()
            if (
                _stable_file_metadata(before) != _stable_file_metadata(after)
                or len(data) > MAX_REVIEW_BYTES
                or path.is_symlink()
            ):
                raise InvalidTarget()
            parent = root
            for part in parts:
                parent = parent / part
                if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
                    raise InvalidTarget()
            return source_path, data.decode("utf-8")
        except (OSError, UnicodeError, ValueError):
            raise InvalidTarget() from None

    @staticmethod
    def _snapshot_posix(root: Path, parts: list[str], limit: int) -> str | None:
        """Pin each directory descriptor and never follow symlinks during open."""
        from .proposal_review import InvalidTarget

        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                _reject_portable_case_alias(os.listdir(directory), part)
                try:
                    child = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                    )
                except FileNotFoundError:
                    return None
                os.close(directory)
                directory = child
            try:
                _reject_portable_case_alias(os.listdir(directory), parts[-1])
                fd = os.open(
                    parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                )
            except FileNotFoundError:
                return None
            with os.fdopen(fd, "rb") as handle:
                before = os.fstat(handle.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                    raise InvalidTarget()
                data = handle.read(limit + 1)
                after = os.fstat(handle.fileno())
                path_after = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
                if (
                    len(data) > limit
                    or _stable_file_metadata(before) != _stable_file_metadata(after)
                    or _stable_file_metadata(before) != _stable_file_metadata(path_after)
                ):
                    raise InvalidTarget()
                return data.decode("utf-8")
        finally:
            os.close(directory)
