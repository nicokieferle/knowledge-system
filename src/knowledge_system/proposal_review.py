"""Client-neutral immutable review values and lifecycle policy; no write capability."""

from __future__ import annotations

import difflib
import hashlib
import logging
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from .client_state import ClientIdentity
from .conversation_models import Proposal

LOG = logging.getLogger(__name__)
MAX_REVIEW_BYTES = 128_000


class ReviewError(RuntimeError):
    """Safe public error: subclasses carry no SQL, paths or private content."""

    def __init__(self):
        super().__init__(type(self).__name__)


class ProposalNotFound(ReviewError):
    pass


class ReviewForbidden(ReviewError):
    pass


class InvalidTransition(ReviewError):
    pass


class ReviewMissing(ReviewError):
    pass


class StaleReview(ReviewError):
    pass


class InvalidTarget(ReviewError):
    pass


class NoDifference(ReviewError):
    pass


class ReviewConflict(ReviewError):
    pass


class InconsistentReview(ReviewError):
    pass


class ProposalStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DEFERRED = "deferred"


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def validate_content(content: str) -> None:
    if not isinstance(content, str):
        raise InvalidTarget()
    try:
        if len(content.encode("utf-8")) > MAX_REVIEW_BYTES:
            raise InvalidTarget()
    except UnicodeError:
        raise InvalidTarget() from None
    # Reject invisible controls/ANSI rather than silently changing the accepted bytes.
    if any(
        ord(c) < 32
        and c not in "\n\r\t"
        or 127 <= ord(c) <= 159
        or unicodedata.category(c) in ("Cf", "Zl", "Zp")
        for c in content
    ):
        raise InvalidTarget()


def unified_diff(path: str, old: str | None, new: str) -> str:
    """Preserve missing final newlines and expose CR bytes as visible escapes."""

    def lines(text: str) -> list[str]:
        # Split only LF (not Unicode separators); escape backslashes before CR/tab.
        parts = text.split("\n")
        raw = [part + "\n" for part in parts[:-1]]
        if parts[-1]:
            raw.append(parts[-1])
        return [
            line.replace("\\", "\\\\").replace("\r", "\\r").replace("\t", "\\t") for line in raw
        ]

    result = []
    for line in difflib.unified_diff(
        lines(old or ""),
        lines(new),
        fromfile="/dev/null" if old is None else "a/" + path,
        tofile="b/" + path,
    ):
        result.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(result)


@dataclass(frozen=True)
class ReviewContent:
    target_source_id: str
    target_source_path: str
    change_kind: str
    base_revision: str
    old_content: str | None
    old_hash: str | None
    new_content: str
    new_hash: str
    diff: str


@dataclass(frozen=True)
class ReviewRevision:
    id: UUID
    proposal_id: UUID
    number: int
    content: ReviewContent
    created_at: datetime


@dataclass(frozen=True)
class ReviewView:
    proposal: Proposal
    revision: ReviewRevision | None
    accepted_review_id: UUID | None


class ReviewSource(Protocol):
    def snapshot(self, source_id: str, path: str) -> tuple[str, str | None]: ...


class ReviewStore(Protocol):
    def proposal_for_revision(self, identity: ClientIdentity, review_id: UUID) -> UUID: ...
    def get(self, identity: ClientIdentity, proposal_id: UUID) -> ReviewView: ...
    def list(self, identity: ClientIdentity, conversation_id: UUID) -> tuple[Proposal, ...]: ...
    def prepare(
        self,
        identity: ClientIdentity,
        proposal_id: UUID,
        content: ReviewContent,
        expected_review_id: UUID | None,
    ) -> ReviewView: ...
    def decide(
        self,
        identity: ClientIdentity,
        proposal_id: UUID,
        review_id: UUID | None,
        status: ProposalStatus,
        expected_status: str,
    ) -> ReviewView: ...


class ProposalReviewService:
    def __init__(self, store: ReviewStore, source: ReviewSource):
        self.store, self.source = store, source

    def get(self, identity: ClientIdentity, proposal_id: UUID) -> ReviewView:
        return self.store.get(identity, proposal_id)

    def decide_revision(
        self, identity: ClientIdentity, review_id: UUID, status: ProposalStatus
    ) -> ReviewView:
        proposal_id = self.store.proposal_for_revision(identity, review_id)
        return self.decide(identity, proposal_id, review_id, status)

    def list(self, identity: ClientIdentity, conversation_id: UUID) -> tuple[Proposal, ...]:
        return self.store.list(identity, conversation_id)

    def prepare(
        self,
        identity: ClientIdentity,
        proposal_id: UUID,
        *,
        target_source_id: str | None = None,
        target_source_path: str | None = None,
        new_content: str | None = None,
    ) -> ReviewView:
        view = self.get(identity, proposal_id)
        if view.proposal.status not in ("pending", "deferred"):
            raise InvalidTransition()
        p = view.proposal
        source_id = target_source_id if target_source_id is not None else p.target_source_id
        path = target_source_path if target_source_path is not None else p.target_source_path
        new = new_content if new_content is not None else p.proposed_content
        validate_content(new)
        if not source_id or not path:
            LOG.warning("review_invalid_target")
            raise InvalidTarget()
        try:
            normalized, old = self.source.snapshot(source_id, path)
        except InvalidTarget:
            LOG.warning("review_invalid_target")
            raise
        if old is not None:
            validate_content(old)
        if old == new or old is None and not new:
            raise NoDifference()
        old_hash = content_hash(old) if old is not None else None
        content = ReviewContent(
            source_id,
            normalized,
            "create" if old is None else "update",
            "absent" if old is None else "sha256:" + old_hash,
            old,
            old_hash,
            new,
            content_hash(new),
            unified_diff(normalized, old, new),
        )
        return self.store.prepare(
            identity, proposal_id, content, view.revision.id if view.revision else None
        )

    def check_basis(self, revision: ReviewRevision) -> None:
        c = revision.content
        _, current = self.source.snapshot(c.target_source_id, c.target_source_path)
        if current != c.old_content:
            LOG.warning("review_stale")
            raise StaleReview()

    def decide(
        self,
        identity: ClientIdentity,
        proposal_id: UUID,
        review_id: UUID | None,
        status: ProposalStatus,
    ) -> ReviewView:
        status = ProposalStatus(status)
        view = self.get(identity, proposal_id)
        if status is ProposalStatus.ACCEPTED:
            if view.revision is None or review_id is None:
                raise ReviewMissing()
            if view.revision.id != review_id:
                raise ReviewConflict()
            # An identical terminal retry does not re-authorize changed source contents.
            if view.proposal.status != "accepted":
                self.check_basis(view.revision)
        return self.store.decide(identity, proposal_id, review_id, status, view.proposal.status)
