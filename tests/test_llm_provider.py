from __future__ import annotations

import io
import json
import urllib.error
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from knowledge_system.conversation_models import ConversationRoutingAction, ConversationTopic
from knowledge_system.llm_provider import (
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
    OpenAICompatibleConfig,
    OpenAICompatibleProvider,
)


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def provider():
    return OpenAICompatibleProvider(OpenAICompatibleConfig("secret", "test-model"))


def test_structured_router_output(monkeypatch):
    result = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "action": "continue_current",
                            "conversation_id": None,
                            "suggested_title": None,
                            "confidence": 0.8,
                        }
                    )
                }
            }
        ]
    }
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *a, **k: Response(json.dumps(result).encode())
    )
    decision = provider().route(
        "Und weiter?",
        (ConversationTopic(__import__("uuid").uuid4(), "Topic", None, datetime.now(UTC), True),),
    )
    assert decision.action is ConversationRoutingAction.CONTINUE_CURRENT


def test_invalid_model_response(monkeypatch):
    result = {"choices": [{"message": {"content": "not-json"}}]}
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *a, **k: Response(json.dumps(result).encode())
    )
    with pytest.raises(LLMResponseError):
        provider().route("x", ())


def test_timeout_and_rate_limit_are_sanitized(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *a, **k: (_ for _ in ()).throw(TimeoutError())
    )
    with pytest.raises(LLMTimeoutError, match="timed out"):
        provider().route("private", ())
    error = urllib.error.HTTPError("https://invalid", 429, "secret", {}, None)
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: (_ for _ in ()).throw(error))
    with pytest.raises(LLMRateLimitError, match="rate limit"):
        provider().route("private", ())


def test_wrapped_timeout_is_distinguishable(monkeypatch):
    error = urllib.error.URLError(TimeoutError("synthetic-private-prompt"))

    def request(*args, **kwargs):
        raise error

    monkeypatch.setattr("urllib.request.urlopen", request)
    with pytest.raises(LLMTimeoutError) as caught:
        provider().route("private", ())
    assert "synthetic-private-prompt" not in str(caught.value)


@pytest.mark.parametrize(
    "body",
    [
        b"not-json-private",
        b"{}",
        b'{"choices": []}',
        b'{"choices": [{"message": {"content": null}}]}',
        b'{"choices": [{"message": {"content": {"private": "data"}}}]}',
        b'{"choices": [{"message": {"content": "  "}}]}',
        b"\xff",
    ],
)
def test_invalid_completion_envelope_is_response_error(monkeypatch, body):
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: Response(body))
    with pytest.raises(LLMResponseError) as caught:
        provider().route("private", ())
    assert "private" not in str(caught.value)


def routing_response(payload):
    return Response(
        json.dumps({"choices": [{"message": {"content": json.dumps(payload)}}]}).encode()
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("conversation_id", "not-a-uuid-PRIVATE"),
        ("conversation_id", ""),
        ("conversation_id", 123),
        ("conversation_id", 0),
        ("conversation_id", 1.5),
        ("conversation_id", True),
        ("conversation_id", False),
        ("conversation_id", {"PRIVATE": "value"}),
        ("conversation_id", []),
        ("action", "PRIVATE"),
        ("action", None),
        ("action", 1),
        ("action", True),
        ("action", {}),
        ("action", []),
        ("confidence", "0.9"),
        ("confidence", "PRIVATE"),
        ("confidence", None),
        ("confidence", True),
        ("confidence", []),
        ("confidence", {}),
        ("confidence", -0.1),
        ("confidence", 1.1),
        ("confidence", float("nan")),
        ("confidence", float("inf")),
        ("confidence", 10**400),
        ("suggested_title", 123),
        ("suggested_title", True),
        ("suggested_title", []),
        ("suggested_title", {"PRIVATE": "value"}),
    ],
)
def test_invalid_routing_field_types_are_controlled(monkeypatch, field, value):
    payload = {
        "action": "continue_current",
        "conversation_id": None,
        "suggested_title": None,
        "confidence": 0.9,
    }
    payload[field] = value
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: routing_response(payload))
    with pytest.raises(LLMResponseError) as caught:
        provider().route("PRIVATE_MESSAGE", ())
    assert str(caught.value) == "LLM returned an invalid routing decision"
    assert caught.value.__suppress_context__ is True


@pytest.mark.parametrize("missing", ["action", "confidence"])
def test_missing_routing_fields_are_controlled(monkeypatch, missing):
    payload = {"action": "continue_current", "confidence": 0.9}
    del payload[missing]
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: routing_response(payload))
    with pytest.raises(LLMResponseError):
        provider().route("PRIVATE_MESSAGE", ())


@pytest.mark.parametrize(
    "action,raw_id,title,confidence",
    [
        ("continue_current", None, None, 0.9),
        ("start_new_conversation", None, "Synthetic title", 1),
        ("switch_to_existing", "d36a229a-3b4e-40f3-aeca-928db94ce468", None, 0.5),
    ],
)
def test_valid_routing_fields(monkeypatch, action, raw_id, title, confidence):
    payload = {
        "action": action,
        "conversation_id": raw_id,
        "suggested_title": title,
        "confidence": confidence,
    }
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: routing_response(payload))
    decision = provider().route("Synthetic", ())
    assert decision.action.value == action
    assert decision.conversation_id == (UUID(raw_id) if raw_id else None)
    assert decision.suggested_title == title
    assert decision.confidence == confidence


@pytest.mark.parametrize(
    "case,action,id_kind,accepted",
    [
        ("active", "continue_current", "active", True),
        ("active", "continue_current", "other", False),
        ("inactive", "continue_current", "active", False),
        ("active", "continue_current", "unoffered", False),
        ("empty", "continue_current", "active", False),
        ("two_active", "continue_current", "active", False),
        ("duplicate_active", "continue_current", "active", False),
        ("conflicting_duplicate", "continue_current", "active", False),
        ("active", "continue_current", "null", True),
        ("active", "switch_to_existing", "other", True),
        ("active", "start_new_conversation", "null", True),
        ("active", "start_new_conversation", "active", False),
    ],
)
def test_routing_normalizes_only_unique_redundant_active_id(
    monkeypatch, case, action, id_kind, accepted
):
    active_id, other_id = uuid4(), uuid4()
    active = ConversationTopic(active_id, "Active", None, datetime.now(UTC), case != "inactive")
    other = ConversationTopic(other_id, "Other", None, datetime.now(UTC), case == "two_active")
    candidates = (active, other)
    if case == "empty":
        candidates = ()
    elif case == "duplicate_active":
        candidates += (active,)
    elif case == "conflicting_duplicate":
        candidates += (ConversationTopic(active_id, "Conflicting", None, datetime.now(UTC)),)
    raw_id = {
        "active": str(active_id),
        "other": str(other_id),
        "unoffered": str(uuid4()),
        "null": None,
    }[id_kind]
    payload = {
        "action": action,
        "conversation_id": raw_id,
        "suggested_title": None,
        "confidence": 0.9,
    }
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: routing_response(payload))
    if not accepted:
        with pytest.raises(LLMResponseError, match="^LLM returned an invalid routing decision$"):
            provider().route("Synthetic", candidates)
        return
    decision = provider().route("Synthetic", candidates)
    assert decision.action.value == action
    assert decision.conversation_id == (other_id if action == "switch_to_existing" else None)
    assert decision.confidence == 0.9


def test_routing_prompt_defines_action_specific_id_contract(monkeypatch):
    def request(req, **kwargs):
        system = json.loads(req.data)["messages"][0]["content"]
        assert "continue_current: conversation_id MUSS null sein" in system
        assert (
            "switch_to_existing: conversation_id MUSS exakt eine angebotene candidate ID sein"
            in system
        )
        assert "start_new_conversation: conversation_id MUSS null sein" in system
        assert "suggested_title" in system and "sonst bevorzugt null" in system
        assert "confidence MUSS eine Zahl zwischen 0 und 1 sein" in system
        return routing_response({"action": "continue_current", "confidence": 0.9})

    monkeypatch.setattr("urllib.request.urlopen", request)
    provider().route("Synthetic", ())


@pytest.mark.parametrize("value", [None, 1, True, {}, [], ""])
def test_proposal_content_must_be_real_text(monkeypatch, value):
    from knowledge_system.conversation_models import ProposalGenerationContext, ProposalTriggerType

    payload = {"summary": "summary", "reason": "reason", "proposed_content": value}
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: routing_response(payload))
    with pytest.raises(LLMResponseError, match="invalid proposal"):
        provider().generate_proposal(
            ProposalGenerationContext(uuid4(), 1, ProposalTriggerType.COMMAND, None, (), ())
        )
