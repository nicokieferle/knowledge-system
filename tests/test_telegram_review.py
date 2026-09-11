from dataclasses import replace
from unittest.mock import Mock

import pytest

from knowledge_system.client_state import ClientIdentity
from knowledge_system.proposal_review import ProposalReviewService, ReviewForbidden
from knowledge_system.sources import GitMarkdownSource
from knowledge_system.telegram_adapter import TelegramCallback, TelegramMessage
from knowledge_system.telegram_review import (
    DIFF_HEADER,
    TELEGRAM_TEXT_UTF16_LIMIT,
    TelegramReview,
    chunks,
    diff_is_inline,
    utf16_units,
)
from tests.test_proposal_review import MemoryReviewStore
from tests.test_telegram_adapter import FakeTransport, adapter_for


def setup_review(tmp_path):
    store = MemoryReviewStore()
    service = ProposalReviewService(store, GitMarkdownSource(tmp_path))
    states, transport = Mock(), FakeTransport()
    who = ClientIdentity("test", "chat", "user")
    return TelegramReview(service, states, transport), store, who, transport


def test_small_diff_is_complete_inline_before_revision_buttons(tmp_path):
    ui, store, who, transport = setup_review(tmp_path)
    view = ui.service.prepare(who, store.view.proposal.id, new_content="small change\n")
    ui.command(who, "/proposal", str(store.view.proposal.id))
    assert len(transport.messages) == 3
    assert transport.documents == []
    assert transport.messages[1][1] == DIFF_HEADER + view.revision.content.diff
    assert all(not buttons for _, _, buttons in transport.messages[:-1])
    buttons = transport.messages[-1][2]
    assert len(buttons) == 3
    assert all(len(data.encode()) <= 64 for _, data in buttons)
    ui.callback(who, "cb", buttons[0][1])
    assert store.view.accepted_review_id == view.revision.id


def test_large_diff_is_one_exact_document_with_constant_api_calls(tmp_path):
    ui, store, who, transport = setup_review(tmp_path)
    store.view = replace(
        store.view,
        proposal=replace(
            store.view.proposal,
            summary="🧪" * 5000,
            reason="reason" * 5000,
            proposed_content="🧪 long synthetic line\n" * 700,
        ),
    )
    ui.command(who, "/proposal", str(store.view.proposal.id))
    revision = store.view.revision
    assert revision is not None
    assert len(transport.messages) == 2
    assert len(transport.documents) == 1
    assert len(transport.messages) + len(transport.documents) == 3
    assert utf16_units(transport.messages[0][1]) <= TELEGRAM_TEXT_UTF16_LIMIT
    chat_id, filename, content, mime_type = transport.documents[0]
    assert chat_id == who.external_chat_id
    assert filename == f"proposal-{store.view.proposal.id.hex[:8]}-review-1.diff"
    assert content == revision.content.diff.encode("utf-8")
    assert mime_type == "text/x-diff"
    assert not transport.messages[0][2]
    assert len(transport.messages[-1][2]) == 3


def test_document_failure_never_sends_decision_buttons(tmp_path):
    ui, store, who, transport = setup_review(tmp_path)
    store.view = replace(
        store.view,
        proposal=replace(store.view.proposal, proposed_content="x\n" * 4000),
    )

    def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic document failure")

    transport.send_document = fail
    with pytest.raises(RuntimeError):
        ui.command(who, "/proposal", str(store.view.proposal.id))
    assert len(transport.messages) == 1
    assert not transport.messages[0][2]


def test_large_diff_retry_reuses_revision_and_may_redeliver_document(tmp_path):
    ui, store, who, transport = setup_review(tmp_path)
    store.view = replace(
        store.view,
        proposal=replace(store.view.proposal, proposed_content="retry\n" * 4000),
    )
    proposal_id = store.view.proposal.id
    ui.command(who, "/proposal", str(proposal_id))
    first = store.view.revision
    ui.command(who, "/proposal", str(proposal_id))
    assert store.view.revision == first
    assert len(store.revisions) == 1
    assert len(transport.documents) == 2
    assert transport.documents[0][2] == transport.documents[1][2]


@pytest.mark.parametrize(
    "data",
    [
        "rv:x:" + "a" * 32,
        "rv:a:bad",
        "rv:a:" + "a" * 33,
        "rv:a:" + "A" * 32,
        "rv:a:" + "a" * 32 + ":extra",
    ],
)
def test_bad_callbacks_never_decide(tmp_path, data):
    ui, store, who, transport = setup_review(tmp_path)
    ui.callback(who, "cb", data)
    assert not store.decisions
    assert "nicht erlaubt" in transport.callbacks[-1][1]


def test_foreign_callback_and_command_are_safe(tmp_path):
    ui, _store, who, transport = setup_review(tmp_path)
    ui.service = Mock()
    ui.service.decide_revision.side_effect = ReviewForbidden()
    ui.callback(who, "cb", "rv:a:" + "a" * 32)
    assert "nicht erlaubt" in transport.callbacks[-1][1]
    assert "ReviewForbidden" not in transport.callbacks[-1][1]


def test_adapter_commands_bypass_router_and_conversation(tmp_path):
    adapter, _, _, states, transport, _, _, _ = adapter_for()
    store = MemoryReviewStore()
    adapter.review = TelegramReview(
        ProposalReviewService(store, GitMarkdownSource(tmp_path)), states, transport
    )
    adapter.router.route = lambda *_: pytest.fail("review command must not route")
    adapter.handle_message(TelegramMessage("1", "2", "3", "/proposals"))
    adapter.handle_message(TelegramMessage("1", "2", "4", f"/proposal {store.view.proposal.id}"))
    buttons = transport.messages[-1][2]
    adapter.handle_callback(TelegramCallback("cb", "1", "2", "4", buttons[2][1]))
    assert store.view.proposal.status == "deferred"


def test_unicode_chunks_roundtrip():
    text = "a🧪\n" * 4000
    assert "".join(chunks(text)) == text


def test_inline_threshold_counts_astral_utf16_units():
    available = TELEGRAM_TEXT_UTF16_LIMIT - utf16_units(DIFF_HEADER)
    assert diff_is_inline("a" * available)
    assert not diff_is_inline("🧪" * available)


def test_invalid_legacy_target_has_no_accept_and_refresh_is_explicit(tmp_path):
    ui, store, who, transport = setup_review(tmp_path)
    store.view = replace(store.view, proposal=replace(store.view.proposal, target_source_path=None))
    ui.command(who, "/proposal", str(store.view.proposal.id))
    buttons = transport.messages[-1][2]
    assert len(transport.messages) == 2
    assert [label for label, _ in buttons] == ["Reject", "Defer"]
    assert not store.revisions
    ui.callback(who, "cb", buttons[0][1])
    assert store.view.proposal.status == "rejected"


def test_refresh_preserves_previous_preview(tmp_path):
    ui, store, who, _ = setup_review(tmp_path)
    ui.command(who, "/proposal", str(store.view.proposal.id))
    first = store.view.revision
    (tmp_path / "notes.md").write_text("changed source", encoding="utf-8")
    ui.command(who, "/proposal", str(store.view.proposal.id))
    assert store.view.revision == first
    ui.command(who, "/proposal-refresh", str(store.view.proposal.id))
    assert store.view.revision.number == 2
    assert store.revisions[0] == first
