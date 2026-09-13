"""Single-process admin sessions. No durable data, passwords or tokens in logs."""

from __future__ import annotations

import os
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from threading import RLock
from urllib.parse import urlsplit

from argon2 import PasswordHasher, extract_parameters
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import Type
from itsdangerous import BadSignature, URLSafeSerializer

from .client_state import ClientIdentity


def internal_target(value: str) -> str:
    """No decoding, authority, fragments or arbitrary query strings allowed."""
    if value == "/reviews" or re.fullmatch(
        r"/reviews/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value
    ):
        return value
    return "/reviews"


def browser_base(value: str) -> str:
    if not value:
        return ""
    parts = urlsplit(value)
    if (
        parts.scheme not in ("https", "http")
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in ("", "/")
        or any(c.isspace() or ord(c) < 32 for c in value)
    ):
        raise ValueError("Invalid REVIEW_BASE_URL")
    if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("REVIEW_BASE_URL requires HTTPS except on loopback")
    return value.rstrip("/")


@dataclass(frozen=True)
class ReviewWebSettings:
    password_hash: str = field(repr=False)
    session_secret: str = field(repr=False)
    owner: ClientIdentity
    mode: str = "production"
    allowed_hosts: tuple[str, ...] = ("127.0.0.1:8080", "localhost:8080")
    session_seconds: int = 3600

    def __post_init__(self):
        if self.mode not in ("production", "development", "test"):
            raise ValueError("Invalid REVIEW_MODE")
        if len(self.session_secret) < 32 or len(set(self.session_secret)) < 12:
            raise ValueError("REVIEW_SESSION_SECRET must be independently random (32+ characters)")
        try:
            p = extract_parameters(self.password_hash)
        except (InvalidHashError, ValueError):
            raise ValueError("REVIEW_PASSWORD_HASH must be a valid Argon2id hash") from None
        if p.type is not Type.ID or p.version != 19:
            raise ValueError("REVIEW_PASSWORD_HASH must use Argon2id version 19")
        if self.mode != "test" and (
            p.memory_cost < 65536 or p.time_cost < 3 or p.salt_len < 16 or p.hash_len < 32
        ):
            raise ValueError("REVIEW_PASSWORD_HASH parameters are too weak")
        if not all(
            (self.owner.client_type, self.owner.external_chat_id, self.owner.external_user_id)
        ):
            raise ValueError("Explicit REVIEW_OWNER mapping is required")
        if not 60 <= self.session_seconds <= 86400:
            raise ValueError("Invalid REVIEW_SESSION_SECONDS")
        if not self.allowed_hosts or any(
            not re.fullmatch(r"[a-zA-Z0-9.\-\[\]:]+", h) for h in self.allowed_hosts
        ):
            raise ValueError("Explicit REVIEW_ALLOWED_HOSTS without wildcards required")

    @property
    def secure(self):
        return self.mode == "production"

    @classmethod
    def from_environment(cls):
        return cls(
            os.getenv("REVIEW_PASSWORD_HASH", ""),
            os.getenv("REVIEW_SESSION_SECRET", ""),
            ClientIdentity(
                os.getenv("REVIEW_OWNER_CLIENT_TYPE", ""),
                os.getenv("REVIEW_OWNER_CHAT_ID", ""),
                os.getenv("REVIEW_OWNER_USER_ID", ""),
            ),
            mode=os.getenv("REVIEW_MODE", "production"),
            allowed_hosts=tuple(
                h.strip().lower()
                for h in os.getenv("REVIEW_ALLOWED_HOSTS", "127.0.0.1:8080,localhost:8080").split(
                    ","
                )
                if h.strip()
            ),
            session_seconds=int(os.getenv("REVIEW_SESSION_SECONDS", "3600")),
        )


@dataclass(frozen=True)
class Session:
    id: str = field(repr=False)
    csrf: str = field(repr=False)
    expires: float
    authenticated: bool


class Sessions:
    """Bounded memory, absolute TTL, restart/logout invalidation; exactly one worker."""

    def __init__(self, settings: ReviewWebSettings, clock=time.monotonic):
        self.settings, self.clock = settings, clock
        self.signer = URLSafeSerializer(settings.session_secret, salt="review-session-v1")
        self.entries: dict[str, Session] = {}
        self.attempts: deque[float] = deque()
        self.lock = RLock()
        self.hasher = PasswordHasher()

    def _expire(self):
        now = self.clock()
        for sid, session in list(self.entries.items()):
            if session.expires <= now:
                del self.entries[sid]

    def create(self, authenticated=False):
        with self.lock:
            self._expire()
            if len(self.entries) >= 256:
                raise OverflowError("Session capacity")
            s = Session(
                secrets.token_urlsafe(32),
                secrets.token_urlsafe(32),
                self.clock() + (self.settings.session_seconds if authenticated else 600),
                authenticated,
            )
            self.entries[s.id] = s
            return s

    def cookie(self, session):
        return self.signer.dumps(session.id)

    def get(self, cookie):
        if not cookie or len(cookie) > 512:
            return None
        try:
            sid = self.signer.loads(cookie)
        except BadSignature:
            return None
        if not isinstance(sid, str):
            return None
        with self.lock:
            self._expire()
            return self.entries.get(sid)

    def discard(self, session):
        with self.lock:
            self.entries.pop(session.id, None)

    def login(self, session, password):
        # One administrator: a global rolling limit avoids IP/proxy spoofing and
        # unbounded per-IP dictionaries. Serialize Argon2 work to bound memory use.
        with self.lock:
            if self.entries.get(session.id) != session or session.expires <= self.clock():
                return None
            now = self.clock()
            while self.attempts and self.attempts[0] <= now - 60:
                self.attempts.popleft()
            if len(self.attempts) >= 5:
                raise OverflowError("Login rate limit")
            self.attempts.append(now)
            try:
                if len(password) > 1024:
                    return None
                self.hasher.verify(self.settings.password_hash, password)
            except (VerificationError, InvalidHashError):
                return None
            self.discard(session)
            return self.create(authenticated=True)
