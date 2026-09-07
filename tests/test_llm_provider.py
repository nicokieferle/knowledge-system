from __future__ import annotations

import io
import json
import urllib.error
from datetime import UTC, datetime

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
