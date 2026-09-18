"""Private browser client; domain writes go exclusively through ProposalReviewService."""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from urllib.parse import parse_qsl, urlencode
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .proposal_apply import ApplyStatus, IndexStatus, ProposalApplyService
from .proposal_review import (
    InvalidTarget,
    InvalidTransition,
    NoDifference,
    ProposalNotFound,
    ProposalReviewService,
    ProposalStatus,
    ReviewConflict,
    ReviewError,
    ReviewForbidden,
    ReviewMissing,
    StaleReview,
)
from .review_auth import ReviewWebSettings, Sessions, internal_target

LOG = logging.getLogger(__name__)
ASSETS = Path(__file__).parent
COOKIE = "review_session"
PAGE_SIZE = 30
STATUS_LABELS = {
    "pending": "Offen",
    "deferred": "Zurückgestellt",
    "accepted": "Akzeptiert",
    "rejected": "Abgelehnt",
}
APPLY_ERROR_LABELS = {
    "basis_changed": "Die Dateibasis hat sich geändert (basis_changed).",
    "basis_hash_changed": "Der Hash der Dateibasis stimmt nicht (basis_hash_changed).",
    "target_changed": "Das Ziel wurde gleichzeitig geändert (target_changed).",
    "case_collision": "Ein gleichnamiger Pfad mit anderer Großschreibung kollidiert (case_collision).",
    "unsafe_permissions": "Eigentümer oder Schreibrechte des Pfads sind nicht sicher (unsafe_permissions).",
    "parent_changed": "Ein übergeordnetes Verzeichnis wurde geändert (parent_changed).",
    "target_invalid": "Das Ziel ist kein sicher lesbares reguläres Dokument (target_invalid).",
    "root_invalid": "Der Knowledge-Root ist nicht sicher erreichbar (root_invalid).",
    "root_changed": "Der Knowledge-Root wurde gleichzeitig ausgetauscht (root_changed).",
    "root_unavailable": "Der Knowledge-Root ist nicht verfügbar (root_unavailable).",
    "review_hash_invalid": "Die gebundene Revision ist inkonsistent (review_hash_invalid).",
    "exchange_detected_third_state": "Ein konkurrierender Drittstand wurde erhalten (exchange_detected_third_state).",
    "recovered_third_state": "Ein Drittstand wurde beim Recovery erhalten (recovered_third_state).",
    "ambiguous_recovery": "Der Wiederherstellungszustand ist uneindeutig (ambiguous_recovery).",
    "filesystem_error": "Der Dateisystemvorgang ist fehlgeschlagen (filesystem_error).",
    "renameat2_unavailable": "Die sichere Linux-Rename-Funktion fehlt (renameat2_unavailable).",
    "verification_failed": "Die Nachprüfung des Schreibvorgangs ist fehlgeschlagen (verification_failed).",
}
INDEX_ERROR_LABELS = {
    "applied_source_changed": "Die angewandte Datei wurde vor der Indexierung geändert (applied_source_changed).",
    "index_operation_failed": "Die dokumentbezogene Indexierung ist fehlgeschlagen (index_operation_failed).",
}
ERRORS = {
    ProposalNotFound: (404, "Vorschlag nicht gefunden."),
    ReviewForbidden: (404, "Vorschlag nicht gefunden."),
    InvalidTarget: (400, "Ungültiges Ziel oder nicht unterstützter Inhalt."),
    NoDifference: (400, "Keine Änderung gegenüber dem aktuellen Ziel."),
    ReviewMissing: (409, "Bitte zuerst eine Review-Revision erzeugen."),
    StaleReview: (409, "Die Basis hat sich geändert. Bitte Review aktualisieren."),
    ReviewConflict: (409, "Revision oder Status wurde inzwischen geändert. Bitte neu laden."),
    InvalidTransition: (409, "Diese Entscheidung ist in diesem Status nicht mehr möglich."),
}


def create_app(
    settings: ReviewWebSettings,
    service: ProposalReviewService,
    apply_service: ProposalApplyService | None = None,
) -> FastAPI:
    if settings.secure and apply_service is None:
        raise ValueError("Production review requires the V3.3 apply service")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    sessions = Sessions(settings)
    app.state.sessions = sessions
    templates = Jinja2Templates(directory=str(ASSETS / "templates"))

    def render(request, template, status=200, **context):
        return templates.TemplateResponse(
            request=request,
            name=template,
            status_code=status,
            context={
                "session": getattr(request.state, "session", None),
                "labels": STATUS_LABELS,
                **context,
            },
        )

    def error(request, status, message):
        return render(request, "error.html", status, message=message)

    def set_cookie(response, session):
        response.set_cookie(
            COOKIE,
            sessions.cookie(session),
            httponly=True,
            secure=settings.secure,
            samesite="strict",
            path="/",
            max_age=settings.session_seconds if session.authenticated else 600,
        )

    @app.middleware("http")
    async def security(request: Request, call_next):
        request.state.session = None
        try:
            if request.headers.get("host", "").lower() not in settings.allowed_hosts:
                response = error(request, 400, "Ungültiger Host.")
            else:
                session = sessions.get(request.cookies.get(COOKIE))
                request.state.session = session
                path = request.url.path
                protected = path == "/reviews" or path.startswith("/reviews/")
                if protected and not (session and session.authenticated):
                    response = (
                        RedirectResponse(
                            "/login?" + urlencode({"next": internal_target(path)}), status_code=303
                        )
                        if request.method in ("GET", "HEAD")
                        else error(request, 401, "Bitte erneut anmelden.")
                    )
                elif request.method == "POST":
                    # Only small URL-encoded forms, no uploads or unbounded parser input.
                    if (
                        request.headers.get("content-type", "").split(";")[0]
                        != "application/x-www-form-urlencoded"
                    ):
                        response = error(request, 400, "Ungültiges Formular.")
                    else:
                        body = bytearray()
                        async for chunk in request.stream():
                            body.extend(chunk)
                            if len(body) > 800_000:
                                break
                        if len(body) > 800_000:
                            response = error(request, 413, "Formular zu groß.")
                        else:
                            pairs = parse_qsl(
                                body.decode("utf-8"),
                                keep_blank_values=True,
                                strict_parsing=True,
                                max_num_fields=10,
                            )
                            form = dict(pairs)
                            request.state.form = form
                            if (
                                len(form) != len(pairs)
                                or not session
                                or not secrets.compare_digest(
                                    form.get("csrf", "").encode(), session.csrf.encode()
                                )
                            ):
                                response = error(
                                    request,
                                    403,
                                    "Formular abgelaufen oder ungültig. Bitte neu laden.",
                                )
                            else:
                                response = await call_next(request)
                else:
                    response = await call_next(request)
        except (ValueError, UnicodeError):
            response = error(request, 400, "Ungültige Eingabe.")
        except OverflowError:
            response = error(
                request, 429, "Zu viele Anfragen. Bitte in einer Minute erneut versuchen."
            )
            response.headers["Retry-After"] = "60"
        except ReviewError as exc:
            status, message = ERRORS.get(type(exc), (500, "Interner Review-Fehler."))
            if status == 500:
                correlation = uuid4().hex
                LOG.error(
                    "review_web_error correlation=%s error=%s", correlation, type(exc).__name__
                )
                message += " Referenz: " + correlation
            response = error(request, status, message)
        except Exception as exc:  # noqa: BLE001 -- boundary must never expose private exceptions
            correlation = uuid4().hex
            LOG.error("review_web_error correlation=%s error=%s", correlation, type(exc).__name__)
            response = error(request, 500, "Interner Fehler. Referenz: " + correlation)
        response.headers.update(
            {
                "Content-Security-Policy": "default-src 'none'; style-src 'self'; script-src 'self'; "
                "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
            }
        )
        return response

    @app.get("/healthz")
    def health():
        return PlainTextResponse("ok")

    @app.get("/", response_class=HTMLResponse)
    def root():
        return RedirectResponse("/reviews", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        session = request.state.session
        if session and session.authenticated:
            return RedirectResponse(internal_target(request.query_params.get("next", "")), 303)
        if not session:
            session = sessions.create()
            request.state.session = session
        response = render(
            request,
            "login.html",
            next=internal_target(request.query_params.get("next", "")),
            failed=False,
            development=not settings.secure,
        )
        set_cookie(response, session)
        return response

    @app.post("/login", response_class=HTMLResponse)
    def login(request: Request):
        form = request.state.form
        session = sessions.login(request.state.session, form.get("password", ""))
        if session is None:
            return render(
                request,
                "login.html",
                401,
                next=internal_target(form.get("next", "")),
                failed=True,
                development=not settings.secure,
            )
        response = RedirectResponse(internal_target(form.get("next", "")), 303)
        set_cookie(response, session)
        return response

    @app.post("/logout")
    def logout(request: Request):
        sessions.discard(request.state.session)
        response = RedirectResponse("/login", 303)
        response.delete_cookie(
            COOKIE, path="/", secure=settings.secure, httponly=True, samesite="strict"
        )
        return response

    @app.get("/reviews", response_class=HTMLResponse)
    def queue(request: Request):
        selection = request.query_params.get("status", "open")
        filters = {
            "open": ("pending", "deferred"),
            "all": tuple(STATUS_LABELS),
            **{s: (s,) for s in STATUS_LABELS},
        }
        if selection not in filters:
            raise ValueError()
        page = int(request.query_params.get("page", "1"))
        result = service.queue(
            settings.owner, filters[selection], (page - 1) * PAGE_SIZE, PAGE_SIZE
        )
        return render(
            request,
            "queue.html",
            result=result,
            selection=selection,
            page=page,
            has_next=page * PAGE_SIZE < result.total,
        )

    @app.get("/reviews/{proposal_id}", response_class=HTMLResponse)
    def detail(request: Request, proposal_id: str):
        view = service.get(settings.owner, UUID(proposal_id))
        application = apply_service.get(settings.owner, view.proposal.id) if apply_service else None
        context = service.context(settings.owner, view.proposal.id)
        mutable = view.proposal.status in ("pending", "deferred")
        stale = False
        if view.revision and mutable:
            try:
                service.check_basis(view.revision)
            except (StaleReview, InvalidTarget):
                stale = True
        return render(
            request,
            "detail.html",
            view=view,
            context=context,
            stale=stale,
            mutable=mutable,
            application=application.apply if application else None,
            apply_status=ApplyStatus,
            index_status=IndexStatus,
            apply_error_labels=APPLY_ERROR_LABELS,
            index_error_labels=INDEX_ERROR_LABELS,
        )

    @app.post("/reviews/{proposal_id}/refresh")
    def refresh(request: Request, proposal_id: str):
        # Explicit preparation only; no preview mutation on GET. Corrected targets
        # still pass the identical source/path/content validation in the domain.
        form = request.state.form
        pid = UUID(proposal_id)
        view = service.get(settings.owner, pid)
        previous = view.revision
        # Native textarea submissions normalize line endings. A basis-only refresh
        # must reuse exact durable bytes, never the browser's textarea serialization.
        candidate = (
            form.get("content", "")
            if form.get("edit_content") == "yes"
            else previous.content.new_content
            if previous
            else view.proposal.proposed_content
        )
        service.prepare(
            settings.owner,
            pid,
            target_source_id="knowledge-git",
            target_source_path=form.get("target") or None,
            new_content=candidate,
            expected_review_id=UUID(form["review_id"]) if form.get("review_id") else None,
        )
        return RedirectResponse(f"/reviews/{pid}", 303)

    @app.post("/reviews/{proposal_id}/decision/{action}")
    def decide(request: Request, proposal_id: str, action: str):
        actions = {
            "reject": ProposalStatus.REJECTED,
            "defer": ProposalStatus.DEFERRED,
        }
        if action not in (*actions, "accept-apply"):
            return error(request, 404, "Seite nicht gefunden.")
        form = request.state.form
        pid, rid = UUID(proposal_id), UUID(form.get("review_id", ""))
        expected = form.get("status", "")
        if expected not in ("pending", "deferred"):
            raise ValueError()
        view = service.get(settings.owner, pid)
        if not view.revision or view.revision.id != rid:
            raise ReviewConflict()
        if action in ("accept-apply", "reject") and form.get("confirmed") != "yes":
            # Explicit confirmation works without JavaScript; retain the original
            # revision/status, never silently substitute a newer review on POST.
            return render(
                request,
                "confirm.html",
                view=view,
                action=action,
                expected=expected,
                label="Accept & Apply"
                if action == "accept-apply"
                else STATUS_LABELS[actions[action].value],
            )
        if action == "accept-apply":
            if apply_service is None:
                raise RuntimeError("Apply service is unavailable")
            apply_service.accept_and_apply(settings.owner, pid, rid, expected)
        else:
            service.decide(settings.owner, pid, rid, actions[action], expected_status=expected)
        return RedirectResponse(f"/reviews/{pid}", 303)

    @app.post("/reviews/{proposal_id}/apply")
    def apply_accepted(request: Request, proposal_id: str):
        if apply_service is None:
            raise RuntimeError("Apply service is unavailable")
        form = request.state.form
        pid = UUID(proposal_id)
        view = service.get(settings.owner, pid)
        rid = UUID(form.get("review_id", ""))
        if view.proposal.status != "accepted" or view.accepted_review_id != rid:
            raise ReviewConflict()
        apply_service.apply_accepted(settings.owner, pid, rid)
        return RedirectResponse(f"/reviews/{pid}", 303)

    @app.post("/reviews/{proposal_id}/retry-apply")
    def retry_apply(request: Request, proposal_id: str):
        if apply_service is None:
            raise RuntimeError("Apply service is unavailable")
        pid = UUID(proposal_id)
        current = apply_service.get(settings.owner, pid)
        if current.apply is None or current.apply.apply_status not in (
            "pending",
            "conflict",
            "failed",
        ):
            raise InvalidTransition()
        apply_service.retry_apply(settings.owner, pid)
        return RedirectResponse(f"/reviews/{pid}", 303)

    @app.post("/reviews/{proposal_id}/retry-index")
    def retry_index(request: Request, proposal_id: str):
        if apply_service is None:
            raise RuntimeError("Apply service is unavailable")
        pid = UUID(proposal_id)
        current = apply_service.get(settings.owner, pid)
        if (
            current.apply is None
            or current.apply.apply_status != "applied"
            or current.apply.index_status != "failed"
        ):
            raise InvalidTransition()
        apply_service.retry_index(settings.owner, pid)
        return RedirectResponse(f"/reviews/{pid}", 303)

    @app.post("/reviews/{proposal_id}/refresh-review")
    def refresh_conflict_review(request: Request, proposal_id: str):
        if apply_service is None:
            raise RuntimeError("Apply service is unavailable")
        pid = UUID(proposal_id)
        current = apply_service.get(settings.owner, pid)
        if current.apply is None or current.apply.apply_status != "conflict":
            raise InvalidTransition()
        refreshed = apply_service.refresh_review(settings.owner, pid)
        return RedirectResponse(f"/reviews/{refreshed.proposal.id}", 303)

    app.mount("/static", StaticFiles(directory=ASSETS / "static"), name="static")
    return app


def main():
    import uvicorn

    from .config import get_settings, validate_apply_knowledge_root
    from .knowledge_apply import SecureKnowledgeWriter
    from .proposal_apply import ProposalApplyService
    from .review_store import PostgresReviewStore
    from .sources import GitMarkdownSource

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # Web config is validated first and never silently loads a development .env.
    web = ReviewWebSettings.from_environment()
    db = get_settings()
    validate_apply_knowledge_root(
        Path(os.getenv("KNOWLEDGE_ROOT", "./knowledge")), production=web.secure
    )
    reviews = PostgresReviewStore(db)
    app = create_app(
        web,
        ProposalReviewService(reviews, GitMarkdownSource(db.knowledge_root)),
        ProposalApplyService(
            db,
            writer=SecureKnowledgeWriter(db.knowledge_root, require_private_permissions=web.secure),
        ),
    )
    uvicorn.run(
        app,
        host=os.getenv("REVIEW_LISTEN_HOST", "127.0.0.1"),
        port=int(os.getenv("REVIEW_PORT", "8080")),
        workers=1,
        access_log=False,
        proxy_headers=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
