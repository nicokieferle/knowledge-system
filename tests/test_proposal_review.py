import os
import stat
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from knowledge_system.client_state import ClientIdentity
from knowledge_system.conversation_models import ProposalDraft, ProposalTriggerType
from knowledge_system.proposal_review import (
    InvalidTarget,
    NoDifference,
    ProposalReviewService,
    ProposalStatus,
    ReviewConflict,
    ReviewMissing,
    ReviewRevision,
    ReviewView,
    StaleReview,
    content_hash,
    unified_diff,
)
from knowledge_system.proposal_store import ProposalCreate
from knowledge_system.sources import GitMarkdownSource, _stable_file_metadata
from tests.conversation_fakes import FakeProposalStore


class MemoryReviewStore:
    """Service fake, not evidence of SQL concurrency or authorization correctness."""

    def __init__(self):
        p = FakeProposalStore().create_proposal(
            ProposalCreate(
                uuid4(),
                "test",
                ProposalTriggerType.COMMAND,
                1,
                (),
                ProposalDraft(
                    "Summary",
                    "Reason",
                    "new\n",
                    target_source_id="knowledge-git",
                    target_source_path="notes.md",
                    base_revision="invented",
                ),
            )
        )
        self.view = ReviewView(p, None, None)
        self.revisions = []
        self.decisions = []

    def get(self, *_):
        return self.view

    def list(self, *_):
        return (self.view.proposal,)

    def proposal_for_revision(self, *_):
        return self.view.proposal.id

    def prepare(self, identity, proposal_id, content, expected_review_id):
        if self.view.revision and self.view.revision.content == content:
            return self.view
        r = ReviewRevision(
            uuid4(), proposal_id, len(self.revisions) + 1, content, datetime.now(UTC)
        )
        self.revisions.append(r)
        self.view = replace(self.view, revision=r)
        return self.view

    def decide(self, identity, proposal_id, review_id, status, expected_status):
        self.decisions.append((review_id, status))
        self.view = replace(
            self.view,
            proposal=replace(self.view.proposal, status=status.value),
            accepted_review_id=review_id if status == ProposalStatus.ACCEPTED else None,
        )
        return self.view


@pytest.fixture
def review(tmp_path):
    store = MemoryReviewStore()
    service = ProposalReviewService(store, GitMarkdownSource(tmp_path))
    return service, store, ClientIdentity("test", "1", "2"), tmp_path


def test_real_basis_immutable_revisions_no_write_and_no_index(review, monkeypatch):
    service, store, who, root = review
    path = root / "notes.md"
    path.write_bytes(b"old\r\n")
    monkeypatch.setattr(
        "knowledge_system.indexer.index_knowledge", lambda *_: pytest.fail("Review must not index")
    )
    first = service.prepare(who, store.view.proposal.id)
    assert first.revision.content.base_revision == "sha256:" + content_hash("old\r\n")
    assert "invented" not in first.revision.content.base_revision
    assert service.prepare(who, store.view.proposal.id) == first
    second = service.prepare(who, store.view.proposal.id, new_content="improved\n")
    assert second.revision.number == 2
    assert store.revisions[0] == first.revision
    with pytest.raises(ReviewConflict):
        service.decide(who, store.view.proposal.id, first.revision.id, ProposalStatus.ACCEPTED)
    accepted = service.decide(
        who, store.view.proposal.id, second.revision.id, ProposalStatus.ACCEPTED
    )
    assert accepted.accepted_review_id == second.revision.id
    assert path.read_bytes() == b"old\r\n"
    assert list(root.iterdir()) == [path]


def test_create_and_stale_and_missing_review(review):
    service, store, who, root = review
    with pytest.raises(ReviewMissing):
        service.decide(who, store.view.proposal.id, None, ProposalStatus.ACCEPTED)
    view = service.prepare(who, store.view.proposal.id)
    assert view.revision.content.change_kind == "create"
    assert view.revision.content.old_hash is None
    assert view.revision.content.base_revision == "absent"
    assert not (root / "notes.md").exists()
    (root / "notes.md").write_text("changed", encoding="utf-8")
    with pytest.raises(StaleReview):
        service.decide(who, store.view.proposal.id, view.revision.id, ProposalStatus.ACCEPTED)
    assert store.decisions == []


def test_noop_and_invalid_source(review):
    service, store, who, root = review
    (root / "notes.md").write_bytes(b"new\n")
    with pytest.raises(NoDifference):
        service.prepare(who, store.view.proposal.id)
    with pytest.raises(InvalidTarget):
        service.prepare(who, store.view.proposal.id, target_source_id="foreign")


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/a.md",
        "C:/a.md",
        "C:\\a.md",
        "../a.md",
        "a/../b.md",
        "a//b.md",
        "./a.md",
        ".private.md",
        "a/.hidden/b.md",
        "README.md",
        "a.txt",
        "",
        "a.md/",
        "a\\b.md",
        "a:stream.md",
        "a /b.md",
        "NUL.md",
        "a/CON.md",
        "a.md\x00",
    ],
)
def test_bad_targets(tmp_path, path):
    with pytest.raises(InvalidTarget):
        GitMarkdownSource(tmp_path).snapshot("knowledge-git", path)


@pytest.mark.parametrize("path", ["readme.md", "ReadMe.md", "README.MD"])
def test_root_readme_case_alias_is_always_excluded(tmp_path, path):
    with pytest.raises(InvalidTarget):
        GitMarkdownSource(tmp_path).snapshot("knowledge-git", path)


@pytest.mark.parametrize(
    "existing,requested",
    [("Notes.md", "notes.md"), ("Folder", "folder/item.md")],
)
def test_existing_component_case_collision_is_rejected(tmp_path, existing, requested):
    existing_path = tmp_path / existing
    if existing_path.suffix:
        existing_path.write_text("existing", encoding="utf-8")
    else:
        existing_path.mkdir()
    with pytest.raises(InvalidTarget):
        GitMarkdownSource(tmp_path).snapshot("knowledge-git", requested)


def _metadata(**changes):
    values = {
        "st_dev": 1,
        "st_ino": 2,
        "st_mode": stat.S_IFREG | 0o600,
        "st_size": 3,
        "st_mtime_ns": 4,
        "st_ctime_ns": 5,
        "st_atime_ns": 6,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_stable_metadata_ignores_only_access_time():
    assert _stable_file_metadata(_metadata()) == _stable_file_metadata(_metadata(st_atime_ns=999))


@pytest.mark.parametrize(
    "change",
    [
        {"st_dev": 9},
        {"st_ino": 9},
        {"st_mode": stat.S_IFDIR | 0o600},
        {"st_size": 9},
        {"st_mtime_ns": 9},
        {"st_ctime_ns": 9},
    ],
)
def test_stable_metadata_detects_relevant_file_changes(change):
    assert _stable_file_metadata(_metadata()) != _stable_file_metadata(_metadata(**change))


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX atime semantics")
def test_posix_snapshot_accepts_atime_change_from_old_access_time(tmp_path):
    path = tmp_path / "notes.md"
    path.write_text("unchanged", encoding="utf-8")
    original = path.stat()
    old_atime = 946684800_000_000_000  # 2000-01-01, old enough to trigger relatime updates.
    os.utime(path, ns=(old_atime, original.st_mtime_ns))
    prepared_atime = path.stat().st_atime_ns
    if prepared_atime != old_atime:
        pytest.skip("filesystem does not preserve the requested old atime")
    result = GitMarkdownSource(tmp_path).snapshot("knowledge-git", "notes.md")
    if path.stat().st_atime_ns == prepared_atime:
        pytest.skip("filesystem does not update atime on read")
    assert result == (
        "notes.md",
        "unchanged",
    )


@pytest.mark.skipif(os.name != "posix", reason="requires descriptor-relative POSIX stat")
def test_posix_snapshot_rejects_path_exchange_during_read(tmp_path, monkeypatch):
    path = tmp_path / "notes.md"
    replacement = tmp_path / "replacement.md"
    path.write_text("original", encoding="utf-8")
    replacement.write_text("replaced", encoding="utf-8")
    real_fstat = os.fstat
    calls = 0

    def exchange_after_read(fd):
        nonlocal calls
        result = real_fstat(fd)
        calls += 1
        if calls == 2:
            os.replace(replacement, path)
        return result

    monkeypatch.setattr(os, "fstat", exchange_after_read)
    with pytest.raises(InvalidTarget):
        GitMarkdownSource(tmp_path).snapshot("knowledge-git", "notes.md")


def test_directory_and_symlink_targets(tmp_path):
    (tmp_path / "dir.md").mkdir()
    with pytest.raises(InvalidTarget):
        GitMarkdownSource(tmp_path).snapshot("knowledge-git", "dir.md")
    outside = tmp_path.parent / ("outside-" + uuid4().hex + ".md")
    outside.write_text("private", encoding="utf-8")
    try:
        (tmp_path / "alias.md").symlink_to(outside)
    except OSError:
        pytest.skip("OS does not permit symlink creation")
    with pytest.raises(InvalidTarget):
        GitMarkdownSource(tmp_path).snapshot("knowledge-git", "alias.md")


def test_diff_newlines_deterministic_and_controls(review):
    diff = unified_diff("a.md", "old", "new\n")
    assert diff == unified_diff("a.md", "old", "new\n")
    assert "-old\n\\ No newline at end of file\n+new\n" in diff
    assert "-old\\r\n" in unified_diff("a.md", "old\r\n", "old\n")
    service, store, who, _ = review
    with pytest.raises(InvalidTarget):
        service.prepare(who, store.view.proposal.id, new_content="\x1b[31mprivate")
