"""Explicit machine-to-review-owner delegation for the private v1 ingress."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .client_state import ClientIdentity


@dataclass(frozen=True)
class IngressSettings:
    client_id: str
    token: str = field(repr=False)
    owner: ClientIdentity
    allowed_hosts: tuple[str, ...] = ("127.0.0.1:8081", "localhost:8081")
    host: str = "127.0.0.1"
    port: int = 8081

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.client_id):
            raise ValueError("Invalid PROPOSAL_INGRESS_CLIENT_ID")
        if (
            len(self.token) < 32
            or not self.token.isascii()
            or any(c.isspace() or not c.isprintable() for c in self.token)
        ):
            raise ValueError("Invalid PROPOSAL_INGRESS_BEARER_TOKEN")
        if not all(
            (self.owner.client_type, self.owner.external_chat_id, self.owner.external_user_id)
        ):
            raise ValueError("Explicit PROPOSAL_INGRESS_OWNER mapping required")
        if not self.allowed_hosts or any(
            not re.fullmatch(r"[A-Za-z0-9.\-\[\]:]+", h) for h in self.allowed_hosts
        ):
            raise ValueError("Explicit PROPOSAL_INGRESS_ALLOWED_HOSTS without wildcards required")
        if not self.host or not 1 <= self.port <= 65535:
            raise ValueError("Invalid PROPOSAL_INGRESS listen address")

    @classmethod
    def from_environment(cls):
        owner = ClientIdentity(
            *(
                os.getenv("PROPOSAL_INGRESS_OWNER_" + k, "")
                for k in ("CLIENT_TYPE", "CHAT_ID", "USER_ID")
            )
        )
        review_owner = ClientIdentity(
            *(os.getenv("REVIEW_OWNER_" + k, "") for k in ("CLIENT_TYPE", "CHAT_ID", "USER_ID"))
        )
        if owner != review_owner:
            raise ValueError("Ingress owner must match REVIEW_OWNER mapping")
        token = os.getenv("PROPOSAL_INGRESS_BEARER_TOKEN", "")
        if token and token in (
            os.getenv("MCP_HTTP_BEARER_TOKEN"),
            os.getenv("REVIEW_SESSION_SECRET"),
            os.getenv("REVIEW_PASSWORD_HASH"),
        ):
            raise ValueError("Ingress requires independent credentials")
        return cls(
            os.getenv("PROPOSAL_INGRESS_CLIENT_ID", ""),
            token,
            owner,
            tuple(
                h.strip().lower()
                for h in os.getenv(
                    "PROPOSAL_INGRESS_ALLOWED_HOSTS", "127.0.0.1:8081,localhost:8081"
                ).split(",")
                if h.strip()
            ),
            os.getenv("PROPOSAL_INGRESS_HOST", "127.0.0.1"),
            int(os.getenv("PROPOSAL_INGRESS_PORT", "8081")),
        )
