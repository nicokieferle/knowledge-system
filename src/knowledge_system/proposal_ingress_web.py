"""Private, bearer-only HTTP submission; no review, filesystem or index service."""

from __future__ import annotations

import hmac
from uuid import UUID, uuid4

import psycopg
from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from .proposal_ingress import IngressError, ProposalIngressService, parse_submission
from .proposal_ingress_config import IngressSettings

MAX_REQUEST_BYTES = 512_000


def error_response(status, code, correlation_id):
    return JSONResponse(
        {"error": code, "correlation_id": correlation_id},
        status_code=status,
        headers={"WWW-Authenticate": "Bearer"} if status == 401 else None,
    )


class IngressBoundary:
    """Authenticate before reading even a byte of the body, including unknown routes."""

    def __init__(self, app, settings):
        self.app, self.settings = app, settings

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        correlation = str(uuid4())
        scope["ingress_correlation"] = correlation
        headers = scope.get("headers", [])
        auth = [v for k, v in headers if k.lower() == b"authorization"]
        alternate = any(
            k.lower() in (b"proxy-authorization", b"x-api-key", b"cookie") for k, _ in headers
        )
        valid = False
        if len(auth) == 1 and not alternate:
            try:
                scheme, token = auth[0].decode("ascii").split(" ", 1)
                valid = scheme.lower() == "bearer" and hmac.compare_digest(
                    token, self.settings.token
                )
            except (UnicodeError, ValueError):
                pass
        if scope["type"] != "http" or not valid:
            return await error_response(401, "unauthorized", correlation)(scope, receive, send)
        hosts = [v.decode("latin1").lower() for k, v in headers if k.lower() == b"host"]
        if len(hosts) != 1 or hosts[0] not in self.settings.allowed_hosts:
            return await error_response(400, "invalid_host", correlation)(scope, receive, send)
        if scope.get("query_string"):
            return await error_response(400, "query_not_allowed", correlation)(scope, receive, send)
        await self.app(scope, receive, send)


def create_app(settings: IngressSettings, service: ProposalIngressService):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error_response(
            exc.status_code,
            "not_found" if exc.status_code == 404 else "method_not_allowed",
            request.scope["ingress_correlation"],
        )

    @app.exception_handler(IngressError)
    async def ingress_error(request, exc):
        return error_response(exc.status, exc.code, request.scope["ingress_correlation"])

    @app.post("/v1/proposals")
    async def submit(request: Request):
        headers = request.scope["headers"]
        keys = [v for k, v in headers if k.lower() == b"idempotency-key"]
        if len(keys) != 1:
            raise IngressError(400, "invalid_idempotency_key")
        try:
            key_text = keys[0].decode("ascii")
            key = UUID(key_text)
            if str(key) != key_text:
                raise ValueError()
        except (ValueError, UnicodeError):
            raise IngressError(400, "invalid_idempotency_key") from None
        media = [v for k, v in headers if k.lower() == b"content-type"]
        if len(media) != 1 or media[0].strip().lower() != b"application/json":
            raise IngressError(415, "unsupported_media_type")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_REQUEST_BYTES:
                raise IngressError(413, "request_too_large")
            body.extend(chunk)
        submission = parse_submission(bytes(body))
        try:
            result = await run_in_threadpool(
                service.submit, settings.client_id, settings.owner, key, submission
            )
        except psycopg.Error:
            raise IngressError(503, "submission_unavailable") from None
        return JSONResponse(result.receipt, status_code=201 if result.created else 200)

    return IngressBoundary(app, settings)


def main():
    import uvicorn

    from .config import get_settings

    # Existing .env loader uses setdefault; explicit process environment wins.
    database = get_settings()
    settings = IngressSettings.from_environment()
    app = create_app(settings, ProposalIngressService(database))
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        workers=1,
        access_log=False,
        proxy_headers=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
