"""Descriptor-relative, crash-recoverable Linux writes for accepted review bytes."""

from __future__ import annotations

import ctypes
import errno
import os
import secrets
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from .proposal_review import MAX_REVIEW_BYTES, InvalidTarget, ReviewRevision, content_hash
from .sources import _reject_portable_case_alias, _stable_file_metadata, validate_source_target


class KnowledgeApplyError(RuntimeError):
    """Internal classified apply error without content or absolute paths."""


class KnowledgeConflict(KnowledgeApplyError):
    pass


class KnowledgeIOFailure(KnowledgeApplyError):
    pass


@dataclass(frozen=True)
class AppliedDocument:
    source_id: str
    source_path: str
    content: str
    sha256: str
    wrote: bool


FaultHook = Callable[[str], None]
RENAME_NOREPLACE = 1
RENAME_EXCHANGE = 2


def _reject_case_alias(names: list[str], requested: str) -> None:
    try:
        _reject_portable_case_alias(names, requested)
    except InvalidTarget:
        # The shared review validator raises its public InvalidTarget type. At
        # apply time, an appearing alias is a changed filesystem basis.
        raise KnowledgeConflict("case_collision") from None


def _renameat2(old_dir: int, old: str, new_dir: int, new: str, flags: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "renameat2", None)
    if function is None:
        raise KnowledgeIOFailure("renameat2_unavailable")
    result = function(
        ctypes.c_int(old_dir),
        ctypes.c_char_p(os.fsencode(old)),
        ctypes.c_int(new_dir),
        ctypes.c_char_p(os.fsencode(new)),
        ctypes.c_uint(flags),
    )
    if result != 0:
        number = ctypes.get_errno()
        if number in (errno.EEXIST, errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
            raise KnowledgeConflict("target_changed")
        raise OSError(number, os.strerror(number))


class SecureKnowledgeWriter:
    """Apply exact accepted bytes without following path components or losing a third state."""

    def __init__(
        self,
        root: Path,
        fault: FaultHook | None = None,
        *,
        require_private_permissions: bool = False,
    ) -> None:
        self.root = root
        self.fault = fault or (lambda _phase: None)
        self.require_private_permissions = require_private_permissions

    def apply(self, apply_id: UUID, revision: ReviewRevision) -> AppliedDocument:
        if os.name != "posix":
            raise KnowledgeIOFailure("posix_required")
        content = revision.content
        parts = validate_source_target(content.target_source_id, content.target_source_path)
        expected = content.new_content.encode("utf-8")
        if (
            len(expected) > MAX_REVIEW_BYTES
            or content_hash(content.new_content) != content.new_hash
        ):
            raise KnowledgeIOFailure("review_hash_invalid")
        try:
            raw_root = self.root.absolute()
            root = raw_root.resolve(strict=True)
            root_stat = os.lstat(raw_root)
            if (
                not stat.S_ISDIR(root_stat.st_mode)
                or stat.S_ISLNK(root_stat.st_mode)
                or root != raw_root
            ):
                raise KnowledgeConflict("root_invalid")
            self._validate_owned_mode(root_stat)
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            opened_root = os.fstat(directory)
            if self._directory_identity(opened_root) != self._directory_identity(root_stat):
                os.close(directory)
                raise KnowledgeConflict("root_changed")
        except KnowledgeApplyError:
            raise
        except (OSError, RuntimeError):
            raise KnowledgeIOFailure("root_unavailable") from None
        try:
            for part in parts[:-1]:
                _reject_case_alias(os.listdir(directory), part)
                try:
                    child = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                    )
                except OSError:
                    raise KnowledgeConflict("parent_changed") from None
                try:
                    self._validate_owned_mode(os.fstat(child))
                except BaseException:
                    os.close(child)
                    raise
                os.close(directory)
                directory = child
            directory_identity = self._directory_identity(os.fstat(directory))

            def verify_directory():
                self._verify_directory_path(raw_root, parts[:-1], directory_identity)

            return self._apply_in_directory(
                directory, apply_id, revision, expected, verify_directory
            )
        except KnowledgeApplyError:
            raise
        except (OSError, UnicodeError):
            raise KnowledgeIOFailure("filesystem_error") from None
        finally:
            os.close(directory)

    def _apply_in_directory(
        self,
        directory: int,
        apply_id: UUID,
        revision: ReviewRevision,
        expected: bytes,
        verify_directory: Callable[[], None],
    ) -> AppliedDocument:
        content = revision.content
        name = content.target_source_path.rsplit("/", 1)[-1]
        prefix = f".knowledge-apply-{apply_id.hex}-"
        verify_directory()
        _reject_case_alias(os.listdir(directory), name)
        self._recover_exchange(directory, name, prefix, content)
        current, metadata, mode = self._read_current(directory, name)
        if current == expected:
            self._cleanup_temps(directory, prefix)
            self._sync_existing(directory, name, expected, verify_directory)
            return AppliedDocument(
                content.target_source_id,
                content.target_source_path,
                content.new_content,
                content.new_hash,
                False,
            )
        old = None if content.old_content is None else content.old_content.encode("utf-8")
        if current != old or (current is None) != (content.change_kind == "create"):
            raise KnowledgeConflict("basis_changed")
        if current is not None and content_hash(current.decode("utf-8")) != content.old_hash:
            raise KnowledgeConflict("basis_hash_changed")
        self.fault("after_basis_read")

        temp = prefix + secrets.token_hex(12) + ".tmp"
        temp_fd = -1
        temp_created = False
        renamed = False
        try:
            temp_fd = os.open(
                temp,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                mode if mode is not None else 0o640,
                dir_fd=directory,
            )
            temp_created = True
            self.fault("after_temp_open")
            view = memoryview(expected)
            while view:
                written = os.write(temp_fd, view)
                if written <= 0:
                    raise OSError(errno.EIO, "short write")
                view = view[written:]
            self.fault("after_temp_write")
            os.fsync(temp_fd)
            self.fault("after_file_fsync")
            os.fchmod(temp_fd, mode if mode is not None else 0o640)
            os.fsync(temp_fd)

            # Recheck the complete configured directory chain and target entry
            # immediately before the atomic operation.
            self.fault("before_rename")
            verify_directory()
            latest, latest_metadata, _ = self._read_current(directory, name)
            if latest != current or latest_metadata != metadata:
                raise KnowledgeConflict("target_changed")
            self.fault("after_target_recheck")
            if current is None:
                _renameat2(directory, temp, directory, name, RENAME_NOREPLACE)
            else:
                _renameat2(directory, temp, directory, name, RENAME_EXCHANGE)
            renamed = True
            self.fault("after_rename")

            if current is not None:
                displaced, displaced_metadata, _ = self._read_current(directory, temp)
                # rename updates ctime on some filesystems. Device, inode, type,
                # size, mtime and exact bytes still identify the displaced base.
                if (
                    displaced != current
                    or displaced_metadata is None
                    or metadata is None
                    or displaced_metadata[:-1] != metadata[:-1]
                ):
                    _renameat2(directory, temp, directory, name, RENAME_EXCHANGE)
                    renamed = False
                    raise KnowledgeConflict("exchange_detected_third_state")
                os.unlink(temp, dir_fd=directory)
            os.fsync(directory)
            self.fault("after_directory_fsync")
            verify_directory()
            verified, _, _ = self._read_current(directory, name)
            if verified != expected:
                raise KnowledgeIOFailure("verification_failed")
            return AppliedDocument(
                content.target_source_id,
                content.target_source_path,
                content.new_content,
                content.new_hash,
                True,
            )
        except KnowledgeApplyError:
            raise
        except OSError:
            raise KnowledgeIOFailure("filesystem_error") from None
        finally:
            if temp_fd >= 0:
                os.close(temp_fd)
            if temp_created and not renamed:
                try:
                    os.unlink(temp, dir_fd=directory)
                except FileNotFoundError:
                    pass

    def _sync_existing(
        self,
        directory: int,
        name: str,
        expected: bytes,
        verify_directory: Callable[[], None],
    ) -> None:
        """Complete file and directory durability after an uncertain prior rename."""

        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except OSError:
            raise KnowledgeConflict("target_changed") from None
        try:
            before = os.fstat(fd)
            self._validate_retry_target(before)
            identity = self._durability_identity(before)
            if self._read_open_file(fd) != expected:
                raise KnowledgeConflict("target_changed")
            if self._durability_identity(os.fstat(fd)) != identity:
                raise KnowledgeConflict("target_changed")

            os.fsync(fd)
            if self._durability_identity(os.fstat(fd)) != identity:
                raise KnowledgeConflict("target_changed")

            # The directory metadata guard also detects an ABA rename away and
            # back around the parent fsync. The target descriptor remains open
            # until all post-fsync identity and byte checks have completed.
            directory_identity = self._durability_identity(os.fstat(directory))
            os.fsync(directory)
            self.fault("after_directory_fsync")
            verify_directory()

            try:
                path_before = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except OSError:
                raise KnowledgeConflict("target_changed") from None
            self._validate_retry_target(path_before)
            if self._durability_identity(path_before) != identity:
                raise KnowledgeConflict("target_changed")

            verified = self._read_open_file(fd)
            descriptor_after = os.fstat(fd)
            try:
                path_after = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except OSError:
                raise KnowledgeConflict("target_changed") from None
            self._validate_retry_target(descriptor_after)
            self._validate_retry_target(path_after)
            if (
                verified != expected
                or self._durability_identity(descriptor_after) != identity
                or self._durability_identity(path_after) != identity
                or self._durability_identity(os.fstat(directory)) != directory_identity
            ):
                raise KnowledgeConflict("target_changed")
        except KnowledgeApplyError:
            raise
        except OSError:
            raise KnowledgeIOFailure("filesystem_error") from None
        finally:
            os.close(fd)

    @staticmethod
    def _durability_identity(info: os.stat_result) -> tuple[int, ...]:
        """Bind retry durability to one unchanged inode and its safety properties."""

        return (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_nlink,
            info.st_uid,
            info.st_gid,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    @staticmethod
    def _read_open_file(fd: int) -> bytes:
        os.lseek(fd, 0, os.SEEK_SET)
        data = bytearray()
        while len(data) <= MAX_REVIEW_BYTES:
            chunk = os.read(fd, min(64 * 1024, MAX_REVIEW_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        return bytes(data)

    def _validate_retry_target(self, info: os.stat_result) -> None:
        if not stat.S_ISREG(info.st_mode) or info.st_nlink < 1 or info.st_size > MAX_REVIEW_BYTES:
            raise KnowledgeConflict("target_invalid")
        self._validate_owned_mode(info)

    def _read_current(
        self, directory: int, name: str
    ) -> tuple[bytes | None, tuple | None, int | None]:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return None, None, None
        except OSError:
            raise KnowledgeConflict("target_invalid") from None
        with os.fdopen(fd, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_REVIEW_BYTES:
                raise KnowledgeConflict("target_invalid")
            self._validate_owned_mode(before)
            data = handle.read(MAX_REVIEW_BYTES + 1)
            after = os.fstat(handle.fileno())
            try:
                path_after = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except OSError:
                raise KnowledgeConflict("target_changed") from None
            stable = _stable_file_metadata(before)
            if (
                len(data) > MAX_REVIEW_BYTES
                or stable != _stable_file_metadata(after)
                or stable != _stable_file_metadata(path_after)
            ):
                raise KnowledgeConflict("target_changed")
            return data, stable, stat.S_IMODE(before.st_mode) & 0o777

    def _recover_exchange(self, directory: int, name: str, prefix: str, content) -> None:
        temps = [entry for entry in os.listdir(directory) if entry.startswith(prefix)]
        if len(temps) > 1:
            raise KnowledgeConflict("ambiguous_recovery")
        if not temps:
            return
        temp = temps[0]
        target, _, _ = self._read_current(directory, name)
        displaced, _, _ = self._read_current(directory, temp)
        new = content.new_content.encode("utf-8")
        old = None if content.old_content is None else content.old_content.encode("utf-8")
        if target == new and displaced == old and old is not None:
            os.unlink(temp, dir_fd=directory)
            os.fsync(directory)
            return
        if target == new and displaced not in (None, old):
            _renameat2(directory, temp, directory, name, RENAME_EXCHANGE)
            os.unlink(temp, dir_fd=directory)
            os.fsync(directory)
            raise KnowledgeConflict("recovered_third_state")
        # If the target is still the exact accepted basis (including explicit
        # absence for a create), no rename took effect. A staging file that is
        # an exact byte prefix of the accepted result is an interrupted write;
        # arbitrary prefix collisions remain untouched and fail closed.
        if target == old and displaced is not None and new.startswith(displaced):
            os.unlink(temp, dir_fd=directory)
            os.fsync(directory)
            return
        raise KnowledgeConflict("ambiguous_recovery")

    @staticmethod
    def _cleanup_temps(directory: int, prefix: str) -> None:
        for entry in os.listdir(directory):
            if entry.startswith(prefix):
                try:
                    info = os.stat(entry, dir_fd=directory, follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode):
                        os.unlink(entry, dir_fd=directory)
                except FileNotFoundError:
                    pass

    @staticmethod
    def _directory_identity(info: os.stat_result) -> tuple[int, int, int]:
        return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)

    def _verify_directory_path(
        self, root: Path, parent_parts: list[str], expected: tuple[int, int, int]
    ) -> None:
        """Confirm the configured path still names the pinned parent directory."""
        try:
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                self._validate_owned_mode(os.fstat(directory))
                for part in parent_parts:
                    _reject_case_alias(os.listdir(directory), part)
                    child = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                    )
                    try:
                        self._validate_owned_mode(os.fstat(child))
                    except BaseException:
                        os.close(child)
                        raise
                    os.close(directory)
                    directory = child
                if self._directory_identity(os.fstat(directory)) != expected:
                    raise KnowledgeConflict("parent_changed")
            finally:
                os.close(directory)
        except KnowledgeApplyError:
            raise
        except (OSError, UnicodeError):
            raise KnowledgeConflict("parent_changed") from None

    def _validate_owned_mode(self, info: os.stat_result) -> None:
        if self.require_private_permissions and (
            info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise KnowledgeConflict("unsafe_permissions")
