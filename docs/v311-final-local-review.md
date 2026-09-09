# V3.1.1 final local review

## Routing-validation follow-up

The second review's malformed-routing blocker is addressed by explicit JSON type
checks for action, conversation_id, suggested_title and confidence. Invalid UUIDs,
types and confidence values become a fixed-message LLMResponseError without an
exposed exception chain. A provider-to-polling regression verifies retry and offset
preservation. No intent checkpoint or schema/workflow change was introduced.

Fresh validation of this follow-up: **232 passed, 0 failed, 0 skipped** in the full
suite, including real isolated PostgreSQL 17.11. The separate PostgreSQL run passed
**13 tests, 0 failed, 0 skipped**. Ruff check, format check, diff check and compileall
passed. These current results are separate from the historical gate below.

## Final gate result (supersedes the historical attempt below)

The subsequent isolated PostgreSQL 17.11 gate passed on commit
`10512691a61dcfd0c411c05afa0be624a34b8b4e`: **13 integration tests passed,
0 failed, 0 skipped**. The full suite then passed **196 tests, 0 failed,
0 skipped**. Durable-state and FK consistency were confirmed. The temporary
container was removed; no production resources or existing volumes were used.
The failed provisioning attempt and its test counts below are historical only.
These numbers describe that completed gate, not a fresh run of later changes.

Processing becomes deterministic once the relevant durable binding/result exists.
Classification before result persistence may be repeated after a failure.
Specifically, a binding fixes the topic; a stored result fixes the outcome.
There is no durable intent checkpoint before that outcome is stored.

## Historical review and provisioning attempt

Reviewed baseline: `98bbcd09bfe4afa1ac96deed8d14b015d340efb4`.
Reviewed initial hotfix: `adf6111c7c17fb141ee6e2741790a44d7c4470ef`.
The complete diff (all 13 files) and the underlying schema, transactions, router,
provider and existing integration smoke were inspected locally.

## Findings

- **Release gate / blocker:** real PostgreSQL verification could not run. Docker
  Desktop is installed but its backend crashed during startup while initializing
  the Inference manager's `dockerInference` Unix socket. WSL's docker-desktop
  distribution stayed stopped. No PostgreSQL binaries were found on PATH or in
  the standard Program Files installation locations. No test container or volume
  was created. Waiting CLI clients were cancelled. No Docker reset, global cleanup,
  production access or repair of existing Docker data was attempted.
- **Medium, corrected:** provider connection timeouts wrapped in `URLError` were
  flattened into `LLMProviderError`; malformed completion envelopes were also
  reported as provider failures. Wrapped timeouts now retain `LLMTimeoutError`;
  malformed JSON/envelopes and invalid/empty content produce `LLMResponseError`.
  No exception text is included in polling logs. Regression tests cover both.
- **Medium, known pre-existing limit:** creation/activation and message binding
  commit separately. An interruption before binding can create an extra unused
  topic and reroute the retried message. Fixing this durably requires an atomic
  routing-decision/binding design, beyond this diagnostic hotfix. A real-DB test
  explicitly exposes this boundary instead of claiming it is idempotent.
- **Medium, known delivery/availability limits:** ambiguous sends can duplicate
  Telegram replies; permanent errors (including expired callback acknowledgements)
  can keep the serial queue blocked. Capped delay bounds frequency, not attempts.
- **Low:** additional indexed reads per message; `process_message` does not identify
  every internal substage. No credential/message/prompt data is logged.
- No additional high-severity regression was established by code review. This is
  not a substitute for the missing PostgreSQL run.

## Persistent boundaries and retry invariants

| Case | Already persistent | Retry / new process | Duplicates or loss | Conversation path |
|---|---|---|---|---|
| A: first attempt | None initially; topic/ownership, binding, user, optional summary, result commit separately | poll → route → binding → user → history/classification → result → send → offset | No atomic transaction across the entire update | Routing model chooses once binding commits |
| B: before any DB effect | Nothing for this update | Model/routing can run again | No DB duplicate or local acknowledgement; model decision may differ | Not deterministic before binding |
| C: after user insert | Topic, ownership, binding, keyed user; summary may also exist | Reuse binding and user; look for result, otherwise classify/process again | One user; LLM work may repeat, intent not fixed until result persists | Bound topic retained |
| D: after routing/binding | Topic and active state; if binding committed, original topic key | Existing binding bypasses routing; if absent, reroute | Before binding: possible extra topic; after binding: no extra topic in serial use | Deterministic only after binding |
| E: after suggestion insert | User and unique suggestion; assistant may be absent | Reuse suggestion; insert keyed confirmation if missing, no classifier call | One suggestion and keyed assistant; historical unkeyed assistants are not migrated | Bound topic retained |
| F: after assistant insert | User and response key; suggestion metadata if applicable | Return saved response without LLM generation | One keyed assistant; another Telegram send is possible | Bound topic retained |
| G: DB complete, send failed | Entire DB outcome; no offset advancement | Replay proposal/suggestion/answer then send again | DB outcome reused; ambiguous delivery may duplicate externally | Bound topic retained |
| H: process restart | Only committed DB state; offset and backoff are in memory | Start offset=0, delay=3; Telegram may redeliver unacknowledged updates; new stores read original bindings/outcomes | Same boundaries as B–G; no deliberate discard | Binding survives process restart |

The next `getUpdates` with an increased offset acknowledges earlier updates to
Telegram. Incrementing the local variable alone is not a durable Telegram ack.
A crash after sending but before the next poll can therefore cause redelivery.
This assumes Telegram still retains the update; indefinite provider downtime can
exceed upstream retention. The bot does not provide an independent durable inbox.

## Transactions and races

The hotfix preserves message, suggestion and proposal uniqueness constraints.
Suggestion confirmation still locks the row and atomically inserts the proposal
and records its resolution. Identical callback retries replay the proposal by its
confirmation-message key; reject retries retain the existing resolved status.
Foreign-key constraints are unchanged. Binding reads do not activate a different
topic or mutate ownership.

Lookup-then-process is not a lock across the whole turn. Multiple workers can race
before binding or classify the same turn into different actions. The guarantee is
serial retries of the single polling worker, not concurrent exactly-once turns.
Proposal lookup by trigger also assumes the normal one-outcome invariant: legacy
rows with different trigger types for the same message are not reconciled.
Manual `/switch` and `/topics` responses are not persisted as turn outcomes.
Old resolved suggestions may replay their original confirmation text/buttons;
the callback store still validates the current resolution state.

## Diagnostic coverage and delivery

The new log reports LLMRateLimitError, LLMTimeoutError, LLMResponseError,
LLMProviderError, HTTPError/URLError, psycopg subclass names and RuntimeError.
HTTP status is emitted only for an actual integer, never by rendering arbitrary
fields. The logger never calls str/repr on exceptions or logs their traceback.
Tests include objects whose str/repr raise, and sentinel secret/payload text.
Unexpected exceptions outside the existing caught set remain outside this hotfix's
retry contract; these logs do not promise classification of every possible failure.

No local DB marker alone can resolve a lost Telegram send acknowledgement: marking
before sending can lose a reply, marking afterward permits duplication. An outbox
can coordinate retries but cannot itself establish exactly-once delivery without
a recipient-side idempotency mechanism. The documented at-least-once limitation
therefore remains; no outbox architecture was added.

## Integration test preparation

`tests/integration/test_telegram_retry_postgres.py` creates a UUID-named schema in
an explicitly supplied loopback `knowledge_v311_test...` database, initializes the
real schema, uses real stores and synthetic models, and drops only that generated
schema in fixture cleanup. It covers activation, binding identity isolation,
external message uniqueness, phase interruptions, fresh store/service replay,
explicit proposals, save/reject callbacks, SQL counts, FK rejection and `/new`.
It also records the pre-binding orphan-topic boundary as an expected limitation.
The existing PostgreSQL smoke remains available for transactional confirmation and
summary races. Neither suite has been claimed to pass against PostgreSQL here.

To finish the gate, provide a working local Docker engine, create a disposable
pgvector-enabled PostgreSQL container bound only to loopback, and set the two test
URLs to its newly created test databases. Run the integration tests and full suite,
then remove only that container and its dedicated storage. No production endpoint
or volume is needed. Push/PR/production-test approval remains pending this gate.

## Historical local validation (before the successful PostgreSQL gate)

- Full pytest suite: **183 passed, 13 skipped**. All 13 skips require PostgreSQL
  (one existing smoke plus twelve newly parameterized retry scenarios).
- `ruff check .`: pass.
- `ruff format --check .`: pass (80 Python files).
- `git diff --check`: pass.
- `python -m compileall -q src`: pass.
- The test run uses a fresh pytest temporary directory and disables cache writes
  because the previously existing Windows pytest temporary/cache paths have
  permission problems. No tests were filtered out of the full suite.
- Final gate: **FAIL / incomplete PostgreSQL verification**, not a demonstrated
  PostgreSQL assertion failure. The new integration tests are collected but have
  not executed against a database. No container, volume or schema cleanup was
  necessary because provisioning never reached database creation.
