"""Local machine authorization boundary for the MCP HTTP transport."""

from __future__ import annotations

import hmac

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class BearerAuthApp:
    def __init__(self, app: ASGIApp, client_id: str, token: str) -> None:
        self.app = app
        self.client_id = client_id
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers", [])
        credentials = [value for name, value in headers if name.lower() == b"authorization"]
        alternate = any(
            name.lower() in {b"proxy-authorization", b"x-api-key"} for name, _ in headers
        )
        valid = False
        if len(credentials) == 1 and not alternate and not scope.get("query_string"):
            try:
                scheme, presented = credentials[0].decode("ascii").split(" ", 1)
                valid = scheme.lower() == "bearer" and hmac.compare_digest(presented, self.token)
            except (UnicodeDecodeError, ValueError):
                pass
        if scope["type"] != "http" or not valid:
            response = PlainTextResponse(
                "Unauthorized", status_code=401, headers={"WWW-Authenticate": "Bearer"}
            )
            await response(scope, receive, send)
            return
        scope["mcp_client_id"] = self.client_id
        await self.app(scope, receive, send)
