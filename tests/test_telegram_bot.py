import urllib.error
from unittest.mock import Mock

import psycopg
import pytest

from knowledge_system import telegram_bot
from knowledge_system.llm_provider import (
    LLMProviderError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
)


class StopPolling(BaseException):
    pass


def update(number=1, callback=False):
    if callback:
        return {
            "update_id": number,
            "callback_query": {
                "id": "private-id",
                "from": {"id": 2},
                "data": "private-data",
                "message": {"chat": {"id": 1}, "message_id": 3},
            },
        }
    return {
        "update_id": number,
        "message": {
            "chat": {"id": 1},
            "from": {"id": 2},
            "message_id": 3,
            "text": "PRIVATE_MESSAGE",
        },
    }


@pytest.mark.parametrize(
    "error",
    [
        LLMRateLimitError("SECRET", http_status=429),
        LLMResponseError("SECRET"),
        LLMProviderError("SECRET", http_status=400),
        psycopg.OperationalError("SECRET"),
        urllib.error.URLError("https://user:SECRET@example.test"),
        urllib.error.HTTPError("https://SECRET", 403, "SECRET", {}, None),
        RuntimeError("SECRET"),
        TimeoutError("SECRET"),
        LLMTimeoutError("SECRET"),
    ],
)
@pytest.mark.parametrize("phase", ["poll", "process_message", "process_callback"])
def test_safe_error_logging(monkeypatch, caplog, error, phase):
    transport, adapter = Mock(), Mock()
    if phase == "poll":
        transport.call.side_effect = [error, StopPolling()]
    else:
        transport.call.side_effect = [[update(callback=phase == "process_callback")], StopPolling()]
        getattr(
            adapter, "handle_callback" if phase == "process_callback" else "handle_message"
        ).side_effect = error
    sleeps = []
    monkeypatch.setattr(telegram_bot.time, "sleep", sleeps.append)
    with pytest.raises(StopPolling):
        telegram_bot.run_polling(adapter, transport)
    assert f"error={type(error).__name__}" in caplog.text
    assert f"phase={phase}" in caplog.text
    assert "SECRET" not in caplog.text
    assert "PRIVATE_MESSAGE" not in caplog.text
    assert "private-data" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    assert sleeps == [3]
    if isinstance(error, LLMProviderError) and error.http_status:
        assert f"http_status={error.http_status}" in caplog.text


def test_backoff_caps_and_resets_only_after_success_and_keeps_offset(monkeypatch):
    transport, adapter = Mock(), Mock()
    # Six failures on the same update; success must reset the next failure to 3s.
    transport.call.side_effect = [[update()] for _ in range(7)] + [[update(2)], StopPolling()]
    adapter.handle_message.side_effect = [LLMRateLimitError("safe") for _ in range(6)] + [
        None,
        RuntimeError("safe"),
    ]
    sleeps = []
    monkeypatch.setattr(telegram_bot.time, "sleep", sleeps.append)
    with pytest.raises(StopPolling):
        telegram_bot.run_polling(adapter, transport)
    assert sleeps == [3, 6, 12, 24, 30, 30, 3]
    assert [call.args[1]["offset"] for call in transport.call.call_args_list] == [0] * 7 + [2, 2]


def test_batch_keeps_successful_offset_when_next_update_fails(monkeypatch):
    transport, adapter = Mock(), Mock()
    transport.call.side_effect = [[update(5), update(6)], StopPolling()]
    adapter.handle_message.side_effect = [None, RuntimeError("safe")]
    monkeypatch.setattr(telegram_bot.time, "sleep", lambda _: None)
    with pytest.raises(StopPolling):
        telegram_bot.run_polling(adapter, transport)
    assert transport.call.call_args.args[1]["offset"] == 6


def test_three_failures_success_success_failure_and_order(monkeypatch):
    transport, adapter = Mock(), Mock()
    first, second, third = update(1), update(2), update(3)
    for number, item in enumerate((first, second, third), 1):
        item["message"]["message_id"] = number
    transport.call.side_effect = [[first, second]] * 4 + [[third], StopPolling()]
    events = []
    attempts = 0

    def handle(message):
        nonlocal attempts
        attempts += 1
        events.append(message.message_id)
        if attempts in (1, 2, 3, 6):
            raise LLMRateLimitError("safe")

    adapter.handle_message.side_effect = handle
    sleeps = []
    monkeypatch.setattr(telegram_bot.time, "sleep", sleeps.append)
    with pytest.raises(StopPolling):
        telegram_bot.run_polling(adapter, transport)
    assert sleeps == [3, 6, 12, 3]
    assert [c.args[1]["offset"] for c in transport.call.call_args_list] == [0, 0, 0, 0, 3, 3]
    assert events == ["1", "1", "1", "1", "2", "3"]


def test_untrusted_exception_fields_are_not_formatted(monkeypatch, caplog):
    class UnsafeText:
        def __str__(self):
            raise AssertionError("must not format exception data")

        def __repr__(self):
            raise AssertionError("must not format exception data")

    error = LLMProviderError(UnsafeText(), http_status=UnsafeText())
    transport = Mock()
    transport.call.side_effect = [error, StopPolling()]
    monkeypatch.setattr(telegram_bot.time, "sleep", lambda _: None)
    with pytest.raises(StopPolling):
        telegram_bot.run_polling(Mock(), transport)
    assert "error=LLMProviderError" in caplog.text
    assert "http_status=unknown" in caplog.text


def test_malformed_routing_response_retries_without_offset_or_log_leak(monkeypatch, caplog):
    from tests.test_llm_provider import provider, routing_response
    from tests.test_telegram_adapter import adapter_for

    adapter, _, store, _, _, _, _, _ = adapter_for()
    adapter.router.model = provider()
    payloads = iter(
        [
            {
                "action": "switch_to_existing",
                "conversation_id": 123,
                "suggested_title": "PRIVATE_PROVIDER_TEXT",
                "confidence": 0.9,
            },
            {
                "action": "continue_current",
                "conversation_id": None,
                "suggested_title": "Synthetic",
                "confidence": 0.9,
            },
        ]
    )
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: routing_response(next(payloads)))
    transport = Mock()
    transport.call.side_effect = [[update()], [update()], StopPolling()]
    sleeps = []
    monkeypatch.setattr(telegram_bot.time, "sleep", sleeps.append)
    with pytest.raises(StopPolling):
        telegram_bot.run_polling(adapter, transport)
    assert [c.args[1]["offset"] for c in transport.call.call_args_list] == [0, 0, 2]
    assert sleeps == [3]
    assert len(store.state.conversations) == 1
    assert "error=LLMResponseError" in caplog.text
    assert "phase=process_message" in caplog.text
    assert "PRIVATE" not in caplog.text
    assert "secret" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_redundant_active_routing_id_binds_original_topic_and_advances_offset(monkeypatch):
    import json

    from knowledge_system.client_state import ClientIdentity
    from knowledge_system.telegram_adapter import TelegramMessage
    from tests.test_llm_provider import provider, routing_response
    from tests.test_telegram_adapter import adapter_for

    adapter, _, store, states, delivery, _, _, chat = adapter_for()
    identity = ClientIdentity("telegram", "1", "2")
    topics = [adapter.router.create(identity, f"Synthetic topic {i}") for i in range(4)]
    active = topics[1]
    states.activate(identity, active.id)
    adapter.router.model = provider()
    requests = []

    def request(req, **kwargs):
        candidates = json.loads(json.loads(req.data)["messages"][1]["content"])["candidates"]
        assert len(candidates) == 4
        assert [c["id"] for c in candidates if c["active"]] == [str(active.id)]
        requests.append(req)
        return routing_response(
            {
                "action": "continue_current",
                "conversation_id": str(active.id),
                "suggested_title": None,
                "confidence": 0.9,
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", request)
    transport = Mock()
    transport.call.side_effect = [[update()], StopPolling()]
    sleeps = []
    monkeypatch.setattr(telegram_bot.time, "sleep", sleeps.append)
    assert states.get_message_binding(identity, "3") is None
    with pytest.raises(StopPolling):
        telegram_bot.run_polling(adapter, transport)
    assert sleeps == []
    assert [c.args[1]["offset"] for c in transport.call.call_args_list] == [0, 2]
    assert states.get_message_binding(identity, "3") == active.id
    assert states.get(identity).active_conversation_id == active.id
    assert set(store.state.conversations) == {t.id for t in topics}
    assert len(store.list_messages(active.id)) == 2
    assert len(chat.contexts) == 1
    assert len(delivery.messages) == 1
    # A repeated update reuses the durable binding/result even if another topic is active.
    states.activate(identity, topics[2].id)
    adapter.handle_message(TelegramMessage("1", "2", "3", "PRIVATE_MESSAGE"))
    assert states.get_message_binding(identity, "3") == active.id
    assert len(requests) == 1
    assert len(chat.contexts) == 1
    assert len(store.list_messages(active.id)) == 2
    assert set(store.state.conversations) == {t.id for t in topics}
