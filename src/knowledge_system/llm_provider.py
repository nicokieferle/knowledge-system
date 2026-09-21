from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from .conversation_models import (
    ConversationContext,
    ConversationIntent,
    ConversationRoutingAction,
    ConversationRoutingDecision,
    ConversationTopic,
    Message,
    ProposalDraft,
    ProposalGenerationContext,
)


class LLMProviderError(RuntimeError):
    """Provider failure whose message deliberately contains no request or secret data."""

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


class LLMResponseError(LLMProviderError):
    pass


class LLMRateLimitError(LLMProviderError):
    pass


class LLMTimeoutError(LLMProviderError):
    pass


@dataclass(frozen=True)
class OpenAICompatibleConfig:
    api_key: str
    model: str
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: float = 30


class OpenAICompatibleProvider:
    """One provider adapter implementing all provider-neutral conversation ports."""

    def __init__(self, config: OpenAICompatibleConfig) -> None:
        if not config.api_key or not config.model:
            raise ValueError("LLM API key and model are required")
        self.config = config

    def generate(self, context: ConversationContext) -> str:
        history = [{"role": m.role.value, "content": m.content} for m in context.recent_messages]
        system = "Du bist der persönliche Knowledge-System-Assistent. Antworte präzise."
        if context.conversation_summary:
            system += f"\nConversation-Zusammenfassung:\n{context.conversation_summary}"
        if context.relevant_knowledge:
            knowledge = "\n\n".join(
                f"[{r.source_id}:{r.source_path}] {r.content}" for r in context.relevant_knowledge
            )
            system += f"\nRelevantes kanonisches Wissen (nur Kontext):\n{knowledge}"
        # The current message is excluded from history by the memory invariant and added once here.
        return self._complete(
            [
                {"role": "system", "content": system},
                *history,
                {"role": "user", "content": context.current_user_message.content},
            ]
        )

    def summarize(self, previous_summary: str | None, messages: tuple[Message, ...]) -> str:
        payload = [{"role": m.role.value, "content": m.content} for m in messages]
        return self._complete(
            [
                {
                    "role": "system",
                    "content": "Fasse den Topic-Kontext knapp und faktentreu zusammen.",
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"previous": previous_summary, "messages": payload}, ensure_ascii=False
                    ),
                },
            ]
        )

    def classify(
        self, current_user_message: Message, recent_messages: tuple[Message, ...]
    ) -> ConversationIntent:
        data = self._json(
            [
                {
                    "role": "system",
                    "content": 'Klassifiziere den Intent als chat, create_proposal, suggest_proposal oder uncertain. create_proposal nur bei Nutzer-Speicherauftrag; suggest_proposal nur ohne Auftrag bei dauerhaft wertvollem Inhalt. Antworte als JSON: {"intent":...}.',
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "recent": [m.content for m in recent_messages[-4:]],
                            "current": current_user_message.content,
                        },
                        ensure_ascii=False,
                    ),
                },
            ]
        )
        try:
            return ConversationIntent(data["intent"])
        except (KeyError, ValueError, TypeError) as exc:
            raise LLMResponseError("LLM returned an invalid intent") from exc

    def route(
        self, current_user_message: str, candidates: tuple[ConversationTopic, ...]
    ) -> ConversationRoutingDecision:
        compact = [
            {
                "id": str(c.conversation_id),
                "title": c.title,
                "summary": c.topic_summary,
                "updated_at": c.updated_at.isoformat(),
                "active": c.is_active,
            }
            for c in candidates
        ]
        data = self._json(
            [
                {
                    "role": "system",
                    "content": (
                        "Route konservativ wie getrennte Chat-Themen. Folgefragen bleiben aktiv. "
                        "Antworte nur JSON mit action, conversation_id, suggested_title, confidence. "
                        "continue_current: conversation_id MUSS null sein. "
                        "switch_to_existing: conversation_id MUSS exakt eine angebotene candidate ID sein. "
                        "start_new_conversation: conversation_id MUSS null sein. "
                        "suggested_title ist nur bei start_new_conversation sinnvoll, sonst bevorzugt null. "
                        "confidence MUSS eine Zahl zwischen 0 und 1 sein."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"candidates": compact, "message": current_user_message}, ensure_ascii=False
                    ),
                },
            ]
        )
        try:
            raw_action = data["action"]
            raw_id = data.get("conversation_id")
            title = data.get("suggested_title")
            confidence = data["confidence"]
            # Validate JSON types before UUID/float conversion. In particular, bool
            # is an int subclass and UUID(non-string) can raise AttributeError.
            if not isinstance(raw_action, str):
                raise TypeError
            if raw_id is not None and not isinstance(raw_id, str):
                raise TypeError
            if title is not None and not isinstance(title, str):
                raise TypeError
            if type(confidence) not in (int, float):
                raise TypeError
            action = ConversationRoutingAction(raw_action)
            conversation_id = UUID(raw_id) if raw_id is not None else None
            if action is ConversationRoutingAction.CONTINUE_CURRENT and conversation_id is not None:
                active = [c for c in candidates if c.is_active]
                # Tolerate only a redundant ID for the unique active candidate.
                # Ambiguous or contradictory candidates must not relax the domain invariant.
                if (
                    len(active) != 1
                    or active[0].conversation_id != conversation_id
                    or sum(c.conversation_id == conversation_id for c in candidates) != 1
                ):
                    raise ValueError
                conversation_id = None
            return ConversationRoutingDecision(
                action,
                conversation_id,
                title,
                float(confidence),
            )
        except (KeyError, ValueError, TypeError, OverflowError):
            raise LLMResponseError("LLM returned an invalid routing decision") from None

    def generate_proposal(self, context: ProposalGenerationContext) -> ProposalDraft:
        data = self._json(
            [
                {
                    "role": "system",
                    "content": (
                        "Erzeuge einen reviewbaren Wissensvorschlag, niemals einen direkten Write. "
                        "JSON-Felder: summary, reason, proposed_content, title, target_source_id, "
                        "target_source_path, base_revision. proposed_content MUSS die vollständige "
                        "neue Markdown-Datei sein, kein Patch und keine Änderungsanweisung. "
                        "target_source_id ist knowledge-git; target_source_path ist ein eindeutiger "
                        "relativer .md-Pfad innerhalb der Wissensquelle, ohne knowledge/-Präfix, "
                        "Traversal oder versteckte Komponenten. Erhalte bestehende Aussagen und "
                        "Provenienz; erfinde keinen bestehenden Dateiinhalt. Bei unklarem Ziel "
                        "liefere null als target_source_path für manuelle Vorbereitung. "
                        "base_revision MUSS null sein; die Review-Schicht bestimmt die reale Basis."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "summary": context.conversation_summary,
                            "messages": [m.content for m in context.originating_messages],
                        },
                        ensure_ascii=False,
                    ),
                },
            ]
        )
        try:
            for field in ("summary", "reason", "proposed_content"):
                if not isinstance(data[field], str) or not data[field].strip():
                    raise TypeError
            for field in ("title", "target_source_id", "target_source_path", "base_revision"):
                if data.get(field) is not None and not isinstance(data[field], str):
                    raise TypeError
            return ProposalDraft(
                summary=data["summary"],
                reason=data["reason"],
                proposed_content=data["proposed_content"],
                title=data.get("title"),
                target_source_id=data.get("target_source_id"),
                target_source_path=data.get("target_source_path"),
                base_revision=data.get("base_revision"),
            )
        except (KeyError, TypeError):
            raise LLMResponseError("LLM returned an invalid proposal") from None

    # ProposalGenerator uses generate(); Python cannot overload it alongside ChatModel. The
    # small wrapper below exposes the expected port while sharing this provider transport.
    def proposal_generator(self) -> ProviderProposalGenerator:
        return ProviderProposalGenerator(self)

    def _json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        try:
            value = json.loads(self._complete(messages, json_mode=True))
        except json.JSONDecodeError as exc:
            raise LLMResponseError("LLM returned malformed JSON") from exc
        if not isinstance(value, dict):
            raise LLMResponseError("LLM returned an invalid structured result")
        return value

    def _complete(self, messages: list[dict[str, str]], json_mode: bool = False) -> str:
        body: dict[str, Any] = {"model": self.config.model, "messages": messages}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        request = urllib.request.Request(
            self.config.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                result = json.load(response)
            content = result["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise LLMResponseError("LLM returned invalid message content")
            return content
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise LLMRateLimitError(
                    "LLM provider rate limit exceeded", http_status=429
                ) from None
            raise LLMProviderError(
                f"LLM provider HTTP error ({exc.code})", http_status=exc.code
            ) from None
        except TimeoutError:
            raise LLMTimeoutError("LLM provider request timed out") from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise LLMTimeoutError("LLM provider request timed out") from None
            raise LLMProviderError("LLM provider request failed") from None
        except (KeyError, IndexError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
            raise LLMResponseError("LLM returned an invalid response") from None


class ProviderProposalGenerator:
    def __init__(self, provider: OpenAICompatibleProvider) -> None:
        self.provider = provider

    def generate(self, context: ProposalGenerationContext) -> ProposalDraft:
        return self.provider.generate_proposal(context)
