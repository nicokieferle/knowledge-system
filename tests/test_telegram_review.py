from unittest.mock import Mock

import pytest

from knowledge_system.client_state import ClientIdentity
from knowledge_system.proposal_review import ProposalReviewService, ReviewForbidden
from knowledge_system.sources import GitMarkdownSource
from knowledge_system.telegram_adapter import TelegramCallback, TelegramMessage
from knowledge_system.telegram_review import TelegramReview, chunks
from tests.test_proposal_review import MemoryReviewStore
from tests.test_telegram_adapter import FakeTransport, adapter_for


def setup_review(tmp_path):
    store = MemoryReviewStore()
    service = ProposalReviewService(store, GitMarkdownSource(tmp_path))
    states, transport = Mock(), FakeTransport()
    who = ClientIdentity("test", "chat", "user")
    return TelegramReview(service, states, transport), store, who, transport


def test_complete_long_diff_and_revision_buttons(tmp_path):
    ui, store, who, transport = setup_review(tmp_path)
    text = "🧪 long synthetic line\n" * 700
    view = ui.service.prepare(who, store.view.proposal.id, new_content=text)
    ui.command(who, "/proposal", str(store.view.proposal.id))
    assert len(transport.messages) > 2
    assert all(len(t.encode("utf-16-le")) // 2 <= 3000 for _, t, _ in transport.messages)
    rendered = "".join(t for _, t, _ in transport.messages[:-1])
    assert view.revision.content.diff in rendered
    assert all(not b for _, _, b in transport.messages[:-1])
    buttons = transport.messages[-1][2]
    assert len(buttons) == 3
    assert all(len(data.encode()) <= 64 for _, data in buttons)
    ui.callback(who, "cb", buttons[0][1])
    assert store.view.accepted_review_id == view.revision.id


def test_partial_delivery_never_sends_accept_button(tmp_path):
    ui, store, who, transport = setup_review(tmp_path)
    ui.service.prepare(who, store.view.proposal.id, new_content="x\n" * 4000)
    sent = []

    def send(chat, text, buttons=()):
        sent.append(buttons)
        if len(sent) == 2:
            raise RuntimeError("synthetic delivery failure")

    transport.send_message = send
    with pytest.raises(RuntimeError):
        ui.command(who, "/proposal", str(store.view.proposal.id))
    assert not any(sent)


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


def test_invalid_legacy_target_has_no_accept_and_refresh_is_explicit(tmp_path):
    from dataclasses import replace

    ui, store, who, transport = setup_review(tmp_path)
    store.view = replace(store.view, proposal=replace(store.view.proposal, target_source_path=None))
    ui.command(who, "/proposal", str(store.view.proposal.id))
    buttons = transport.messages[-1][2]
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
