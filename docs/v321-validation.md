# V3.2.1 validation and focused security review

Validated locally on 2026-09-13. Base:
`1aab528c10fb8a90b1694187ccb0d02e2e743e05` (PR #7 included).
No production access, deployment, merge or real Telegram messages.

## Final gates

| Gate | Result |
| --- | --- |
| Full Windows pytest, with isolated PostgreSQL opt-ins | 374 passed, 0 failed, 3 skipped (58.44 s) |
| Real PostgreSQL integration tests within that run | 38 passed, PostgreSQL 17.11 |
| Linux source/web/Telegram review regression run | 90 passed, 0 skipped (4.99 s) |
| Ruff check | PASS |
| Ruff format check | PASS, 98 files |
| Compileall src and tests | PASS |
| Git diff check | PASS |
| Compose configuration and safe bind/default checks | PASS |
| Packaged templates/static assets and container health | PASS |
| Changed-file secret/artifact inspection | PASS; synthetic test credentials only |
| Canonical knowledge changes | None |

The three Windows skips are platform-specific filesystem tests (POSIX atime,
descriptor-relative exchange and symlink creation). The Linux run exercises the
corresponding source tests without skips; the target platform is not hidden.
The Linux run has one third-party AnyIO/Starlette deprecation warning, not a test
failure. Test temp/cache directories were isolated to avoid inherited Windows
temporary-directory permissions. No tests were disabled to obtain a green gate.

PostgreSQL used a uniquely named local test container with tmpfs database storage,
synthetic credentials and separate test databases. Tests cover browser ownership,
provenance, pagination, actual row-lock/concurrent decisions, immutable revisions,
status/revision compare-and-set, retries and the previous Telegram/domain regressions.
There is no schema or durable-state change, so no new migration or restore format
was needed. Existing backup/restore tests ran as part of the full suite; this task
does not claim a new production restore exercise.

The actual Dockerfile was built locally. A final local image layer reinstalled the
final source wheel without downloading the same dependencies again. The web process
was healthy with UID 10001, read-only root, no capabilities, no network and a tmpfs
temporary directory. Login, Secure cookie and packaged CSS were checked inside it.
Health means HTTP process readiness; database connectivity is deliberately not
exposed by the unauthenticated health route.

Both task-owned PostgreSQL and web-health containers were stopped and removed after
verification. Existing containers and persistent volumes were untouched. Local test
image tags remain as build cache; no prune or global cleanup was performed.

## Browser checks

Synthetic local data only: desktop and 390-pixel smartphone layouts, empty and
populated queue, long target path, small and large complete diff, stale warning,
accepted/rejected terminal views, wrong password, session expiry and parallel tabs.
The large diff contained 52,073 displayed characters. Long content did not widen the
mobile page. A second tab could not overwrite a completed decision and received a
safe conflict response. The empty queue displayed zero open proposals correctly.
Temporary UI servers/tabs were closed; no screenshots were committed.

## Separate security self-review

No confirmed blocker or high-severity issue remains. Checked:

- Telegram legacy `rv:`/`pv:` callbacks return before any review lookup or mutation;
  commands expose neither details, diffs/documents nor decision controls.
- Web mutations call the existing domain service. Owner comes only from configured
  principal; foreign resources return 404. GET does not prepare or decide.
- Decisions bind the displayed immutable review and observed status under the
  existing transaction/lock. Refresh also rejects a superseded displayed revision.
- Ordinary refresh preserves durable candidate bytes: HTML textarea newline
  normalization is used only after explicit opt-in to edit the candidate.
- All POSTs require session-bound CSRF. Login rotates sessions; logout/expiry
  invalidates them. Production uses Secure/HttpOnly/Strict cookies and fails closed
  on absent or inadequate auth configuration. Argon2 work and session memory are bounded.
- Jinja escapes untrusted context, paths and diffs; restrictive headers, exact Host
  allowlist and internal-only redirect targets cover the browser boundary.
- No sensitive application log payloads, CDN, tracking, SPA, public default bind,
  Docker socket, privileged mode, knowledge writer or index capability was added.
- MCP, retrieval, source-path validation and durable backup/restore logic are unchanged.

Known boundaries: one worker/in-memory sessions, global login throttling can be
exhausted temporarily by another private-network client, and offset pagination can
shift across separate requests during concurrent inserts. Production private HTTPS
and WireGuard configuration remain a later operator-controlled deployment step.
Review consent is not atomic filesystem apply; V3.3 must revalidate source bytes at
apply time. Reject/defer retain the existing domain policy allowing stale candidates.

## Changed files

- `.env.server.example`
- `README.md`
- `compose.server.yml`
- `pyproject.toml`
- `docs/DEPLOYMENT_DEBIAN.md`
- `docs/ROADMAP.md`
- `docs/conversation-architecture.md`
- `docs/v32-review-design.md`
- `docs/v321-browser-review.md`
- `docs/v321-validation.md`
- `src/knowledge_system/conversation_service.py`
- `src/knowledge_system/proposal_review.py`
- `src/knowledge_system/review_store.py`
- `src/knowledge_system/review_auth.py`
- `src/knowledge_system/review_web.py`
- `src/knowledge_system/static/review.css`
- `src/knowledge_system/static/review.js`
- `src/knowledge_system/templates/base.html`
- `src/knowledge_system/templates/confirm.html`
- `src/knowledge_system/templates/detail.html`
- `src/knowledge_system/templates/error.html`
- `src/knowledge_system/templates/login.html`
- `src/knowledge_system/templates/queue.html`
- `src/knowledge_system/telegram_adapter.py`
- `src/knowledge_system/telegram_bot.py`
- `src/knowledge_system/telegram_review.py`
- `tests/integration/test_proposal_review_postgres.py`
- `tests/integration/test_review_web_postgres.py`
- `tests/review_web_demo.py`
- `tests/test_deployment.py`
- `tests/test_review_web.py`
- `tests/test_telegram_adapter.py`
- `tests/test_telegram_review.py`
