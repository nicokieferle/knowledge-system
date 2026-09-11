"""Thin rendering/parsing layer. Authorization and decisions belong to the service."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import TYPE_CHECKING
from uuid import UUID

from .client_state import ClientIdentity, ClientStateStore
from .proposal_review import InvalidTarget, NoDifference, ProposalStatus, ReviewError, ReviewMissing

if TYPE_CHECKING:
    from .proposal_review import ProposalReviewService
    from .telegram_adapter import TelegramTransport

TELEGRAM_TEXT_UTF16_LIMIT = 3000
DIFF_HEADER = "Diff (\\r, \\t und \\\\ sind sichtbar escaped):\n"


def utf16_units(text: str) -> int:
    return sum(2 if ord(character) > 0xFFFF else 1 for character in text)


def chunks(text: str, limit: int = TELEGRAM_TEXT_UTF16_LIMIT) -> Iterator[str]:
    # Telegram counts UTF-16 units; astral code points require two units.
    part, size = [], 0
    for c in text:
        units = 2 if ord(c) > 0xFFFF else 1
        if size + units > limit:
            yield "".join(part)
            part, size = [], 0
        part.append(c)
        size += units
    if part:
        yield "".join(part)


def truncate_utf16(text: str, limit: int) -> str:
    if utf16_units(text) <= limit:
        return text
    marker = "…"
    return next(chunks(text, limit - utf16_units(marker))) + marker


def diff_is_inline(diff: str) -> bool:
    return utf16_units(DIFF_HEADER + diff) <= TELEGRAM_TEXT_UTF16_LIMIT


class TelegramReview:
    def __init__(
        self,
        service: ProposalReviewService,
        states: ClientStateStore,
        transport: TelegramTransport,
    ) -> None:
        self.service, self.states, self.transport = service, states, transport

    def command(self, identity: ClientIdentity, command: str, argument: str) -> None:
        try:
            if command == "/proposals":
                state = self.states.get(identity)
                proposals = (
                    self.service.list(identity, state.active_conversation_id)
                    if (state and state.active_conversation_id)
                    else ()
                )
                text = "\n".join(f"{p.id} [{p.status}] {p.summary[:160]}" for p in proposals)
                for part in chunks(text or "Keine offenen Vorschläge im aktiven Topic."):
                    self.transport.send_message(identity.external_chat_id, part)
                return
            if not re.fullmatch(r"[0-9a-fA-F-]{36}", argument):
                raise ValueError
            proposal_id = UUID(argument)
            view = self.service.get(identity, proposal_id)
            if (
                view.revision is None or command == "/proposal-refresh"
            ) and view.proposal.status in ("pending", "deferred"):
                try:
                    view = self.service.prepare(identity, proposal_id)
                except (InvalidTarget, NoDifference):
                    if view.revision is not None:
                        raise
                    p = view.proposal
                    self.transport.send_message(
                        identity.external_chat_id,
                        f"{truncate_utf16(p.summary, 400)}\n"
                        f"Grund: {truncate_utf16(p.reason, 800)}\nStatus: {p.status}\n"
                        "Kein reviewbarer Entwurf. Ziel/Inhalt muss korrigiert werden.",
                    )
                    self.transport.send_message(
                        identity.external_chat_id,
                        "Ohne Review kein Accept.",
                        (("Reject", f"pv:r:{p.id.hex}"), ("Defer", f"pv:d:{p.id.hex}")),
                    )
                    return
            if view.revision is None:
                raise ReviewMissing()
            p, r = view.proposal, view.revision
            summary = (
                f"{truncate_utf16(p.title or p.summary, 400)}\n"
                f"Grund: {truncate_utf16(p.reason, 800)}\nStatus: {p.status}\n"
                f"Ziel: {r.content.target_source_path}\nReview: {r.number} ({r.id})\n"
                f"Basis: {r.content.base_revision}"
            )
            self.transport.send_message(identity.external_chat_id, summary)
            if diff_is_inline(r.content.diff):
                self.transport.send_message(identity.external_chat_id, DIFF_HEADER + r.content.diff)
            else:
                filename = f"proposal-{p.id.hex[:8]}-review-{r.number}.diff"
                self.transport.send_document(
                    identity.external_chat_id,
                    filename,
                    r.content.diff.encode("utf-8"),
                )
            # Only after the full inline diff or document was sent successfully.
            buttons = ()
            if p.status in ("pending", "deferred"):
                buttons = tuple(
                    (label, f"rv:{action}:{r.id.hex}")
                    for label, action in (("Accept", "a"), ("Reject", "r"), ("Defer", "d"))
                )
            self.transport.send_message(
                identity.external_chat_id,
                f"Review {r.number}: Entscheidung gilt exakt für diese Revision. "
                "Accept speichert nur die Zustimmung, ohne Git-Write.",
                buttons,
            )
        except (ReviewError, ValueError):
            self.transport.send_message(
                identity.external_chat_id,
                "Vorschlag nicht verfügbar oder nicht reviewbar. Ziel/Basis prüfen lassen.",
            )

    def callback(self, identity: ClientIdentity, callback_id: str, data: str) -> None:
        try:
            bare = re.fullmatch(r"pv:([rd]):([0-9a-f]{32})", data)
            if bare:
                action, raw_id = bare.groups()
                status = ProposalStatus.REJECTED if action == "r" else ProposalStatus.DEFERRED
                view = self.service.decide(identity, UUID(raw_id), None, status)
                self.transport.answer_callback(
                    callback_id, f"Status: {view.proposal.status}. Kein Git-Write."
                )
                return
            match = re.fullmatch(r"rv:([ard]):([0-9a-f]{32})", data)
            if match is None:
                raise ValueError
            action, raw_id = match.groups()
            status = {
                "a": ProposalStatus.ACCEPTED,
                "r": ProposalStatus.REJECTED,
                "d": ProposalStatus.DEFERRED,
            }[action]
            view = self.service.decide_revision(identity, UUID(raw_id), status)
            text = f"Status: {view.proposal.status}. Kein Git-Write."
        except (ReviewError, ValueError):
            text = "Aktion nicht erlaubt oder Review veraltet. Vorschlag erneut anzeigen."
        self.transport.answer_callback(callback_id, text)
