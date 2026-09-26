from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import psycopg

from .client_state import PostgresClientStateStore
from .config import get_chat_client_settings, get_settings
from .conversation_memory import ConversationContextBuilder, ConversationMemory
from .conversation_router import ConversationRouter
from .conversation_service import ConversationService
from .conversation_store import PostgresConversationStore
from .llm_provider import LLMProviderError, OpenAICompatibleConfig, OpenAICompatibleProvider
from .proposal_service import ProposalService
from .proposal_store import PostgresProposalStore
from .service import KnowledgeService
from .telegram_adapter import TelegramAdapter, TelegramCallback, TelegramMessage

LOG = logging.getLogger(__name__)


class TelegramHTTPTransport:
    def __init__(self, token: str, timeout: float = 35) -> None:
        self._base = f"https://api.telegram.org/bot{token}/"
        self.timeout = timeout

    def call(self, method: str, payload: dict[str, Any]) -> Any:
        request = urllib.request.Request(
            self._base + method,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        return self._execute(request)

    def _execute(self, request: urllib.request.Request) -> Any:
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            result = json.load(response)
        if not result.get("ok"):
            raise RuntimeError("Telegram API request failed")
        return result["result"]

    def send_message(
        self, chat_id: str, text: str, buttons: tuple[tuple[str, str], ...] = ()
    ) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": label, "callback_data": data} for label, data in buttons]
                ]
            }
        self.call("sendMessage", payload)

    def send_document(
        self,
        chat_id: str,
        filename: str,
        content: bytes,
        mime_type: str = "text/x-diff",
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", filename):
            raise ValueError("Invalid Telegram document filename")
        if mime_type not in ("text/x-diff", "text/plain") or not isinstance(content, bytes):
            raise ValueError("Invalid Telegram document")
        fields = (str(chat_id).encode("utf-8"), filename.encode("ascii"), mime_type.encode("ascii"))
        for _ in range(8):
            boundary = ("knowledge-system-" + secrets.token_hex(24)).encode("ascii")
            if all(boundary not in value for value in (*fields, content)):
                break
        else:
            raise RuntimeError("Could not create safe multipart boundary")
        body = b"".join(
            (
                b"--" + boundary + b'\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n',
                fields[0],
                b"\r\n--"
                + boundary
                + b'\r\nContent-Disposition: form-data; name="document"; filename="',
                fields[1],
                b'"\r\nContent-Type: ' + fields[2] + b"\r\n\r\n",
                content,
                b"\r\n--" + boundary + b"--\r\n",
            )
        )
        request = urllib.request.Request(
            self._base + "sendDocument",
            data=body,
            headers={"Content-Type": "multipart/form-data; boundary=" + boundary.decode("ascii")},
        )
        self._execute(request)

    def answer_callback(self, callback_id: str, text: str) -> None:
        self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


def build_adapter() -> tuple[TelegramAdapter, TelegramHTTPTransport]:
    settings, chat = get_settings(), get_chat_client_settings()
    provider = OpenAICompatibleProvider(
        OpenAICompatibleConfig(
            chat.llm_api_key, chat.llm_model, chat.llm_base_url, chat.llm_timeout_seconds
        )
    )
    conversations = PostgresConversationStore(settings)
    states = PostgresClientStateStore(settings)
    proposals = PostgresProposalStore(settings)
    memory = ConversationMemory(conversations, provider)
    proposal_service = ProposalService(conversations, proposals, provider.proposal_generator())
    service = ConversationService(
        conversation_store=conversations,
        context_builder=ConversationContextBuilder(memory),
        proposal_service=proposal_service,
        chat_model=provider,
        intent_classifier=provider,
        knowledge_retriever=KnowledgeService(settings, verbose=False),
    )
    transport = TelegramHTTPTransport(chat.telegram_bot_token)
    return TelegramAdapter(
        ConversationRouter(conversations, states, provider),
        service,
        states,
        transport,
        review_base_url=os.getenv("REVIEW_BASE_URL", ""),
    ), transport


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    adapter, transport = build_adapter()
    run_polling(adapter, transport)


def run_polling(adapter: TelegramAdapter, transport: TelegramHTTPTransport) -> None:
    offset = 0
    retry_delay = 3
    LOG.info("Telegram long-polling client started")
    while True:
        phase = "poll"
        try:
            updates = transport.call(
                "getUpdates",
                {"offset": offset, "timeout": 30, "allowed_updates": ["message", "callback_query"]},
            )
            for update in updates:
                if message := update.get("message"):
                    phase = "process_message"
                    if text := message.get("text"):
                        adapter.handle_message(
                            TelegramMessage(
                                str(message["chat"]["id"]),
                                str(message["from"]["id"]),
                                str(message["message_id"]),
                                text,
                            )
                        )
                elif callback := update.get("callback_query"):
                    phase = "process_callback"
                    message = callback.get("message")
                    if message and callback.get("data"):
                        adapter.handle_callback(
                            TelegramCallback(
                                str(callback["id"]),
                                str(message["chat"]["id"]),
                                str(callback["from"]["id"]),
                                str(message["message_id"]),
                                str(callback["data"]),
                            )
                        )
                # Advance only after durable handling; provider/DB failures retry this update.
                offset = max(offset, int(update["update_id"]) + 1)
                retry_delay = 3
            retry_delay = 3
        except (
            urllib.error.URLError,
            TimeoutError,
            LLMProviderError,
            psycopg.Error,
            RuntimeError,
        ) as exc:
            # Never render arbitrary exception messages or tracebacks: they can contain
            # request URLs, SQL parameters or prompts. Class names suffice for diagnosis.
            status = exc.code if isinstance(exc, urllib.error.HTTPError) else None
            if isinstance(exc, LLMProviderError):
                status = exc.http_status
            LOG.warning(
                "Telegram polling cycle failed error=%s phase=%s http_status=%s retry_seconds=%s",
                type(exc).__name__,
                phase,
                status if type(status) is int else "unknown",
                retry_delay,
            )
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 30)


if __name__ == "__main__":
    main()
