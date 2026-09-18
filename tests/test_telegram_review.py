"""Telegram has no review capability, including replayed historic buttons."""

from unittest.mock import Mock

import pytest

from knowledge_system.telegram_adapter import TelegramCallback, TelegramMessage
from knowledge_system.telegram_review import NOTICE
from tests.test_telegram_adapter import adapter_for


@pytest.mark.parametrize(
    "action",
    [
        "rv:a",
        "rv:r",
        "rv:d",
        "rv:refresh",
        "rv:apply",
        "rv:index",
        "pv:r",
        "pv:d",
        "pv:refresh",
        "pv:apply",
    ],
)
def test_legacy_callbacks_never_reach_any_domain_service(action):
    adapter, _, _, _, transport, _, _, _ = adapter_for()
    adapter.service = Mock()
    adapter.states = Mock()
    adapter.router = Mock()
    callback = TelegramCallback("old", "chat", "user", "message", action + ":" + "a" * 32)
    adapter.handle_callback(callback)
    adapter.handle_callback(callback)
    assert adapter.service.mock_calls == []
    assert adapter.states.mock_calls == []
    assert adapter.router.mock_calls == []
    assert transport.callbacks == [("old", NOTICE), ("old", NOTICE)]
    assert not transport.messages and not transport.documents


@pytest.mark.parametrize("command", ["/proposals", "/proposal", "/proposal-refresh"])
def test_legacy_commands_are_information_only(command):
    adapter, _, _, _, transport, _, _, _ = adapter_for()
    adapter.service, adapter.router, adapter.states = Mock(), Mock(), Mock()
    adapter.handle_message(TelegramMessage("1", "2", "3", command + " " + "a" * 32))
    assert transport.messages == [("1", NOTICE, ())]
    assert not transport.documents
    assert not adapter.service.mock_calls and not adapter.router.mock_calls
    assert not adapter.states.mock_calls
