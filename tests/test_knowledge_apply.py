from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from knowledge_system.knowledge_apply import (
    KnowledgeConflict,
    KnowledgeIOFailure,
    SecureKnowledgeWriter,
)
from knowledge_system.proposal_review import (
    InvalidTarget,
    ReviewContent,
    ReviewRevision,
    content_hash,
)


def revision(path: str, old: str | None, new: str) -> ReviewRevision:
    return ReviewRevision(
        uuid4(),
        uuid4(),
        1,
        ReviewContent(
            "knowledge-git",
            path,
            "create" if old is None else "update",
            "absent" if old is None else "sha256:" + content_hash(old),
            old,
            content_hash(old) if old is not None else None,
            new,
            content_hash(new),
            "display-only",
        ),
        datetime.now(UTC),
    )


@pytest.mark.skipif(os.name != "posix", reason="V3.3 apply requires Linux descriptor APIs")
def test_update_is_exact_atomic_preserves_mode_and_retry_is_idempotent(tmp_path):
    target = tmp_path / "notes.md"
    target.write_bytes(b"old\r\n")
    target.chmod(0o640)
    item = revision("notes.md", "old\r\n", "new\n")
    apply_id = uuid4()
    writer = SecureKnowledgeWriter(tmp_path)

    first = writer.apply(apply_id, item)
    second = writer.apply(apply_id, item)

    assert first.wrote is True and second.wrote is False
    assert target.read_bytes() == b"new\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert not list(tmp_path.glob(".knowledge-apply-*"))


@pytest.mark.skipif(os.name != "posix", reason="V3.3 apply requires Linux descriptor APIs")
def test_create_never_overwrites_target_that_appears(tmp_path):
    item = revision("new.md", None, "accepted\n")

    def fault(phase):
        if phase == "after_target_recheck":
            (tmp_path / "new.md").write_text("third\n", encoding="utf-8")

    with pytest.raises(KnowledgeConflict):
        SecureKnowledgeWriter(tmp_path, fault).apply(uuid4(), item)
    assert (tmp_path / "new.md").read_text(encoding="utf-8") == "third\n"
    assert not list(tmp_path.glob(".knowledge-apply-*"))


@pytest.mark.skipif(os.name != "posix", reason="V3.3 apply requires Linux descriptor APIs")
def test_update_detects_external_exchange_before_rename_without_lost_update(tmp_path):
    target = tmp_path / "notes.md"
    target.write_text("old\n", encoding="utf-8")
    item = revision("notes.md", "old\n", "accepted\n")

    def fault(phase):
        if phase == "after_target_recheck":
            replacement = tmp_path / "external.md"
            replacement.write_text("third\n", encoding="utf-8")
            os.replace(replacement, target)

    with pytest.raises(KnowledgeConflict):
        SecureKnowledgeWriter(tmp_path, fault).apply(uuid4(), item)
    assert target.read_text(encoding="utf-8") == "third\n"


class SimulatedCrash(BaseException):
    pass


@pytest.mark.skipif(os.name != "posix", reason="V3.3 apply requires Linux descriptor APIs")
def test_crash_after_exchange_is_recovered_as_idempotent_success(tmp_path):
    target = tmp_path / "notes.md"
    target.write_text("old\n", encoding="utf-8")
    item = revision("notes.md", "old\n", "accepted\n")
    apply_id = uuid4()

    def crash(phase):
        if phase == "after_rename":
            raise SimulatedCrash()

    with pytest.raises(SimulatedCrash):
        SecureKnowledgeWriter(tmp_path, crash).apply(apply_id, item)
    assert target.read_text(encoding="utf-8") == "accepted\n"
    assert len(list(tmp_path.glob(".knowledge-apply-*"))) == 1

    recovered = SecureKnowledgeWriter(tmp_path).apply(apply_id, item)
    assert recovered.wrote is False
    assert target.read_text(encoding="utf-8") == "accepted\n"
    assert not list(tmp_path.glob(".knowledge-apply-*"))


@pytest.mark.skipif(os.name != "posix", reason="V3.3 apply requires Linux descriptor APIs")
def test_partial_temp_left_by_process_loss_is_cleaned_and_retried(tmp_path):
    target = tmp_path / "notes.md"
    target.write_text("old\n", encoding="utf-8")
    item = revision("notes.md", "old\n", "accepted\n")
    apply_id = uuid4()

    # Simulate SIGKILL/power loss, where Python's finally cleanup cannot run.
    partial = tmp_path / f".knowledge-apply-{apply_id.hex}-{'a' * 24}.tmp"
    partial.write_bytes(b"acce")

    recovered = SecureKnowledgeWriter(tmp_path).apply(apply_id, item)
    assert recovered.wrote is True
    assert target.read_text(encoding="utf-8") == "accepted\n"
    assert not list(tmp_path.glob(".knowledge-apply-*"))


@pytest.mark.skipif(os.name != "posix", reason="V3.3 apply requires Linux descriptor APIs")
@pytest.mark.parametrize(
    "phase", ["after_temp_open", "after_temp_write", "after_file_fsync", "before_rename"]
)
def test_pre_rename_io_failures_leave_original_and_clean_temp(tmp_path, phase):
    target = tmp_path / "notes.md"
    target.write_text("old\n", encoding="utf-8")
    item = revision("notes.md", "old\n", "accepted\n")

    def fail(current):
        if current == phase:
            raise KnowledgeIOFailure("injected_io_failure")

    with pytest.raises(KnowledgeIOFailure):
        SecureKnowledgeWriter(tmp_path, fail).apply(uuid4(), item)
    assert target.read_text(encoding="utf-8") == "old\n"
    assert not list(tmp_path.glob(".knowledge-apply-*"))


@pytest.mark.skipif(os.name != "posix", reason="V3.3 apply requires Linux descriptor APIs")
@pytest.mark.parametrize("operation", ["write", "file_fsync"])
def test_real_temp_io_errors_leave_original_and_clean_temp(tmp_path, monkeypatch, operation):
    target = tmp_path / "notes.md"
    target.write_text("old\n", encoding="utf-8")
    item = revision("notes.md", "old\n", "accepted\n")

    if operation == "write":
        monkeypatch.setattr(os, "write", lambda _fd, _data: (_ for _ in ()).throw(OSError()))
    else:
        monkeypatch.setattr(os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError()))

    with pytest.raises(KnowledgeIOFailure, match="filesystem_error"):
        SecureKnowledgeWriter(tmp_path).apply(uuid4(), item)
    assert target.read_text(encoding="utf-8") == "old\n"
    assert not list(tmp_path.glob(".knowledge-apply-*"))


@pytest.mark.skipif(os.name != "posix", reason="V3.3 apply requires Linux descriptor APIs")
@pytest.mark.parametrize("old", [None, "old\n"])
def test_transient_directory_fsync_error_is_resynchronized_on_retry(tmp_path, monkeypatch, old):
    target = tmp_path / "notes.md"
    if old is not None:
        target.write_text(old, encoding="utf-8")
    item = revision("notes.md", old, "accepted\n")
    apply_id = uuid4()
    real_fsync = os.fsync
    directory_calls = 0
    regular_calls = 0

    def fail_directory_fsync(fd):
        nonlocal directory_calls, regular_calls
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_calls += 1
            if directory_calls == 1:
                raise OSError()
        else:
            regular_calls += 1
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)
    with pytest.raises(KnowledgeIOFailure, match="filesystem_error"):
        SecureKnowledgeWriter(tmp_path).apply(apply_id, item)
    assert target.read_text(encoding="utf-8") == "accepted\n"

    recovered = SecureKnowledgeWriter(tmp_path).apply(apply_id, item)
    assert recovered.wrote is False
    assert directory_calls == 2
    assert regular_calls == 3
    assert not list(tmp_path.glob(".knowledge-apply-*"))


@pytest.mark.skipif(os.name != "posix", reason="V3.3 apply requires Linux descriptor APIs")
@pytest.mark.parametrize("old", [None, "old\n"])
def test_persistent_directory_fsync_error_keeps_retry_failed(tmp_path, monkeypatch, old):
    target = tmp_path / "notes.md"
    if old is not None:
        target.write_text(old, encoding="utf-8")
    item = revision("notes.md", old, "accepted\n")
    apply_id = uuid4()
    real_fsync = os.fsync
    directory_calls = 0
    regular_calls = 0

    def fail_directory_fsync(fd):
        nonlocal directory_calls, regular_calls
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_calls += 1
            raise OSError()
        regular_calls += 1
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)
    for _ in range(2):
        with pytest.raises(KnowledgeIOFailure, match="filesystem_error"):
            SecureKnowledgeWriter(tmp_path).apply(apply_id, item)

    assert target.read_text(encoding="utf-8") == "accepted\n"
    assert directory_calls == 2
    assert regular_calls == 3


@pytest.mark.skipif(os.name != "posix", reason="V3.3 apply requires Linux descriptor APIs")
def test_changed_bytes_symlink_parent_case_alias_and_missing_parent_are_conflicts(tmp_path):
    item = revision("folder/notes.md", "old\n", "new\n")
    with pytest.raises(KnowledgeConflict):
        SecureKnowledgeWriter(tmp_path).apply(uuid4(), item)
    (tmp_path / "Folder").mkdir()
    with pytest.raises(KnowledgeConflict, match="case_collision"):
        SecureKnowledgeWriter(tmp_path).apply(uuid4(), item)
    (tmp_path / "Folder").rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "folder"
    try:
        alias.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(KnowledgeConflict):
        SecureKnowledgeWriter(tmp_path).apply(uuid4(), item)
    alias.unlink()
    (tmp_path / "Note.md").write_text("old\n", encoding="utf-8")
    with pytest.raises(KnowledgeConflict, match="case_collision"):
        SecureKnowledgeWriter(tmp_path).apply(uuid4(), revision("note.md", None, "new\n"))
    (tmp_path / "Note.md").unlink()
    outside_file = tmp_path / "outside.md"
    outside_file.write_text("old\n", encoding="utf-8")
    target_alias = tmp_path / "alias.md"
    target_alias.symlink_to(outside_file)
    with pytest.raises(KnowledgeConflict):
        SecureKnowledgeWriter(tmp_path).apply(uuid4(), revision("alias.md", "old\n", "new\n"))


@pytest.mark.skipif(os.name != "posix", reason="V3.3 apply requires Linux descriptor APIs")
def test_basis_bytes_and_metadata_are_rechecked(tmp_path):
    target = tmp_path / "notes.md"
    target.write_text("old\n", encoding="utf-8")
    item = revision("notes.md", "old\n", "new\n")

    def mutate(phase):
        if phase == "after_basis_read":
            target.write_text("changed\n", encoding="utf-8")

    with pytest.raises(KnowledgeConflict):
        SecureKnowledgeWriter(tmp_path, mutate).apply(uuid4(), item)
    assert target.read_text(encoding="utf-8") == "changed\n"


@pytest.mark.skipif(os.name != "posix", reason="V3.3 apply requires Linux descriptor APIs")
def test_parent_exchange_during_write_is_detected_before_rename(tmp_path):
    parent = tmp_path / "folder"
    parent.mkdir()
    target = parent / "notes.md"
    target.write_text("old\n", encoding="utf-8")
    item = revision("folder/notes.md", "old\n", "new\n")

    def exchange_parent(phase):
        if phase == "before_rename":
            parent.rename(tmp_path / "moved")
            parent.mkdir()
            (parent / "notes.md").write_text("third\n", encoding="utf-8")

    with pytest.raises(KnowledgeConflict, match="parent_changed"):
        SecureKnowledgeWriter(tmp_path, exchange_parent).apply(uuid4(), item)
    assert (parent / "notes.md").read_text(encoding="utf-8") == "third\n"
    assert (tmp_path / "moved" / "notes.md").read_text(encoding="utf-8") == "old\n"
    assert not list((tmp_path / "moved").glob(".knowledge-apply-*"))


def test_apply_rejects_traversal_and_absolute_paths(tmp_path):
    for path in ("../outside.md", "/tmp/outside.md", "a/../outside.md"):
        with pytest.raises(InvalidTarget):
            SecureKnowledgeWriter(tmp_path).apply(uuid4(), revision(path, None, "new"))


@pytest.mark.skipif(os.name != "posix", reason="V3.3 apply requires Linux descriptor APIs")
def test_writer_rejects_a_symlinked_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(KnowledgeConflict, match="root_invalid"):
        SecureKnowledgeWriter(alias).apply(uuid4(), revision("new.md", None, "new\n"))


@pytest.mark.skipif(os.name != "posix", reason="V3.3 apply requires Linux descriptor APIs")
def test_writer_rejects_group_writable_target(tmp_path):
    target = tmp_path / "notes.md"
    target.write_text("old\n", encoding="utf-8")
    target.chmod(0o660)
    with pytest.raises(KnowledgeConflict, match="unsafe_permissions"):
        SecureKnowledgeWriter(tmp_path, require_private_permissions=True).apply(
            uuid4(), revision("notes.md", "old\n", "new\n")
        )


@pytest.mark.skipif(os.name != "posix", reason="V3.3 apply requires Linux descriptor APIs")
def test_inode_change_with_identical_bytes_conflicts(tmp_path):
    target = tmp_path / "notes.md"
    target.write_text("old\n", encoding="utf-8")
    item = revision("notes.md", "old\n", "new\n")

    def exchange_same_bytes(phase):
        if phase == "after_basis_read":
            replacement = tmp_path / "replacement.md"
            replacement.write_text("old\n", encoding="utf-8")
            original = target.stat()
            os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
            os.replace(replacement, target)

    with pytest.raises(KnowledgeConflict):
        SecureKnowledgeWriter(tmp_path, exchange_same_bytes).apply(uuid4(), item)


@pytest.mark.skipif(os.name != "posix", reason="V3.3 apply requires Linux descriptor APIs")
def test_temp_collision_is_not_removed_and_partial_writes_complete(tmp_path, monkeypatch):
    target = tmp_path / "notes.md"
    target.write_text("old\n", encoding="utf-8")
    item = revision("notes.md", "old\n", "accepted bytes\n")
    apply_id = uuid4()
    token = "a" * 24
    collision = tmp_path / f".knowledge-apply-{apply_id.hex}-{token}.tmp"
    collision.write_text("do not remove", encoding="utf-8")
    monkeypatch.setattr("knowledge_system.knowledge_apply.secrets.token_hex", lambda _size: token)
    with pytest.raises(KnowledgeConflict, match="ambiguous_recovery"):
        SecureKnowledgeWriter(tmp_path).apply(apply_id, item)
    assert collision.read_text(encoding="utf-8") == "do not remove"
    collision.unlink()

    real_write = os.write
    writes = 0

    def partial(fd, data):
        nonlocal writes
        writes += 1
        return real_write(fd, data[: max(1, len(data) // 2)])

    monkeypatch.setattr(os, "write", partial)
    result = SecureKnowledgeWriter(tmp_path).apply(apply_id, item)
    assert result.wrote is True and writes > 1
    assert target.read_bytes() == b"accepted bytes\n"
