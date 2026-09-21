# V3.2.1 — private Browser-Review-Konsole

> V3.2.1 history. V3.3 keeps its authentication, ownership, CSRF and full-diff
> guarantees but changes the pending acceptance route to
> `POST /reviews/{uuid}/decision/accept-apply`. It durably binds the decision/intent
> and then applies/indexes as described in [V3.3](v33-design.md). Reject/defer remain
> decision-only. Old accepted proposals require the separate explicit Apply action.
> No V3.3 mutation is available through Telegram or MCP.

Base: `1aab528c10fb8a90b1694187ccb0d02e2e743e05`, including PR #7.
Development only: no merge, deployment, server access or real Telegram messages.

## Architecture and product boundary

Telegram collects pending proposals, with a clear queue confirmation. It does not
send review details, full diffs/documents, or decision buttons. Existing `rv:` and
`pv:` callbacks return information before any lookup or mutation. The three old
review commands similarly only explain the browser-only policy. Optional links use
REVIEW_BASE_URL, a validated trusted origin; links convey no authorization.

FastAPI/Jinja2 renders German HTML with local CSS and a tiny duplicate-submit UX
script; no CDN, tracking, Node build or SPA. Review routes call the existing
ProposalReviewService, never issue SQL status changes themselves. MCP and retrieval
are unchanged. Accept stores consent to the specific revision, never writes a file.

## Authentication and sessions

One admin, no registration. REVIEW_PASSWORD_HASH is an Argon2id v19 hash. Production
requires at least 64 MiB, time cost 3 and a 16-byte salt; argon2-cffi verifies it.
REVIEW_SESSION_SECRET is an independent random secret of at least 32 characters.
Generate both locally in a trusted terminal, never send passwords in CLI arguments:

```sh
python -c "from getpass import getpass; from argon2 import PasswordHasher; print(PasswordHasher().hash(getpass('Review password: ')))"
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Put output only into the protected, ignored `.env.server`; quote the hash with
single quotes so Compose does not interpolate its dollar signs. No generated secret
belongs in Git. Examples intentionally contain no default admin password or hash.

Cookies contain an itsdangerous-signed random session identifier, never principal
data. Server memory holds authentication and a random CSRF token. Cookies are
HttpOnly, SameSite Strict, Path=/, and Secure in production. Login rotates both ID
and CSRF token; logout invalidates the server record. Auth sessions expire absolutely
after REVIEW_SESSION_SECONDS (default 3600, range 60–86400); login sessions after ten
minutes. Restart or secret rotation invalidates all sessions. Exactly one worker:
do not add replicas/workers without a shared session and throttle design.

Memory is capped at 256 sessions. Expired records are removed before capacity is
evaluated. When a new anonymous login session reaches the cap, only the oldest
anonymous session is evicted; authenticated sessions are never evicted for a login
page. A store containing only authenticated sessions fails closed. A global rolling
limit of five password checks per minute bounds Argon2 work and prevents
IP/proxy-header spoofing of the throttle.
An attacker on the private network can temporarily exhaust login capacity; this is
a deliberate small single-admin tradeoff, not a public Internet authentication system.

All POSTs including login/logout require a session-bound CSRF token. Missing,
incorrect, expired and foreign-session tokens fail closed. Tokens are reusable within
the session; durable decision idempotency handles legitimate retries. Passwords,
cookies, CSRF values, messages and diff contents are never application log fields.
HTTP access logging and trusted proxy-header inference are disabled.

## Owner and provenance

Set REVIEW_OWNER_CLIENT_TYPE=telegram and REVIEW_OWNER_CHAT_ID/REVIEW_OWNER_USER_ID
to the exact existing client_conversations tuple for the administrator. No IDs are
guessed or accepted from forms. Other owners' proposal IDs return the same 404 as
missing IDs. The queue uses all owned conversations, not the current/recent 20 topics.

Existing originating_message_ids identify stored source messages. Only user messages
from the authorized proposal conversation are loaded; explicit control metadata is
excluded. No full-conversation copy, extra snapshot, schema change or durable session
table is added. Older proposals without usable provenance show an unavailable notice.
These messages are context only, never automatically copied into candidate Markdown.

## Routes and concurrency

- GET/POST `/login`; POST `/logout`.
- GET `/reviews`: pending/deferred by default; all/status filter, 30 rows/page,
  deterministic created_at/id ordering, explicit previous/next links and open count.
  Count and page share a repeatable-read snapshot. Offset is bounded at one million
  (larger requests fail explicitly, never silently hide entries). Concurrent additions
  can shift page boundaries between requests; a fresh queue view reconciles them.
- GET `/reviews/{uuid}` never prepares or changes data. It renders full provenance,
  old/new text, diff, revision IDs, basis/hash references and stale warning.
- POST `/reviews/{uuid}/refresh` explicitly prepares from current source with optional
  corrected target/candidate, through unchanged safe path/content checks. Identical
  candidate/base reuses the revision. Old immutable revisions remain stored.
  Refresh binds the displayed revision ID and rejects a superseded tab. Candidate
  text comes from durable bytes unless the separate edit checkbox is checked;
  this avoids HTML textarea newline normalization during an ordinary basis refresh.
  Explicit edits use browser-normalized line endings and must be reviewed again.
- POST `/reviews/{uuid}/accept|reject|defer` requires the displayed revision ID and
  observed pending/deferred status. Accept/reject first show a separate confirmation
  page; it retains both original identifiers and works with JavaScript disabled.
  Final POST uses the existing row lock, ownership checks and expected-status CAS.
  Identical retries reuse audit records; superseded revisions/parallel decisions
  produce 409. Completed actions redirect (303); reloading GET cannot repeat them.

Accept rechecks basis; reject/defer preserve the existing policy permitting disposition
of a stale candidate. Terminal proposals have no action controls. A browser decision
always needs a prepared concrete revision, including rejection of legacy drafts.
V3.3 must recheck source bytes atomically during apply: filesystem and PostgreSQL
are not one transaction, and review consent itself has no filesystem-write capability.

## Browser safety and network

Untrusted content is Jinja-autoescaped plain text, never Markdown-to-HTML or innerHTML.
Diff escapes retain the domain display format; each old/new content remains limited
to 128,000 UTF-8 bytes. POST parsing bounds bytes and fields and rejects duplicate
fields. CSP restricts scripts/styles/forms to self and prohibits framing/base changes;
responses also set nosniff, DENY, no-referrer and no-store. Host matching uses an exact
configured allowlist. Login return URLs only allow the queue or canonical UUID detail
path; no external URL, arbitrary query, fragment or owner is reflected into redirects.

Default native bind is loopback. Compose host_ip defaults to loopback even if unset
or empty. REVIEW_LISTEN_HOST=0.0.0.0 applies only inside the container. WireGuard-only
is the intended future access boundary, not something configured here. Production
needs private HTTPS termination and a corresponding explicit allowed Host; do not
switch production into development mode to work around Secure cookies. Include
127.0.0.1:8080 in REVIEW_ALLOWED_HOSTS for the internal healthcheck. No public port,
cloud tunnel, firewall, reverse proxy, DNS or WireGuard change is authorized here.

## Local start and checks

Install `pip install -e '.[dev]'`. In the current shell, explicitly set REVIEW_MODE
to `development`, synthetic local hash/secret, the three REVIEW_OWNER fields, and
DATABASE_URL for an initialized isolated PostgreSQL-17 database. Set KNOWLEDGE_ROOT
to a temporary synthetic source directory, not production data. Then run
`knowledge-review` (127.0.0.1:8080). Test mode only relaxes hash cost for unit tests;
it never supplies a default password or bypasses login. No startup schema/index run.

`/healthz` returns only `ok`; it verifies the HTTP process, not live DB connectivity.
The Compose service waits for PostgreSQL health at start. Database failures later
return safe error pages with correlation IDs, not SQL details or stacktraces.

Run `pytest tests/test_review_web.py tests/test_telegram_review.py`; real browser/DB
tests are in `tests/integration/test_review_web_postgres.py` and use the existing
strict loopback `TEST_V32_DATABASE_URL` opt-in. Full final validation is recorded
separately after the gate completes. No deployment or canonical apply is a test step.
