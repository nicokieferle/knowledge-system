# V3.2 local validation and self-review

Validated on 2026-09-11, based on main
`92b25474b94a3d4a369e50363cc3df68a2fba62c`. Remote main was fetched and
remained unchanged. This is development validation, not a production rollout.

## Results

- Full Windows suite with all three PostgreSQL opt-ins: **304 passed, 1 skipped**
  (32.53 seconds on the final run).
- All **27 PostgreSQL integration cases** executed, including 14 V3.2 cases and
  the 13 existing conversation/retry cases. No PostgreSQL skips or failures.
- PostgreSQL **17.11**, isolated local Docker test container, loopback-only port,
  tmpfs data directory, synthetic credentials/data, separate test databases.
- Linux review/path suite: **24 passed**, including the symlink test skipped on
  Windows because Windows did not permit symlink creation. The minimal Linux
  image emitted a missing sentence-transformers dependency warning; this was a
  focused source/path suite, not a full Linux application test.
- `python -m ruff check .`: PASS.
- `python -m ruff format --check .`: PASS (89 files).
- `python -m compileall src tests`: PASS.
- `git diff --check`: PASS.
- `knowledge/` unchanged; no MCP tool changes. Existing tests verify exactly two
  read-only MCP tools. Review/Accept tests prohibit index invocation and verify
  original file bytes remain unchanged.

The pytest invocation used a fresh temporary basetemp/cache directory due local
permissions. `TEST_DATABASE_URL`, `TEST_V311_DATABASE_URL`,
`TEST_V32_DATABASE_URL`, and `TEST_V32_CONTAINER` enabled all local DB tests.
Each V3.0 smoke run used a fresh database, since that smoke requires an empty DB.

Migration tests start from the actual V3.1.2 schema retrieved with `git show`,
preserve an existing pending proposal, and repeat initialization. Real transaction
tests cover ownership, exact revision binding, terminal/idempotent decisions,
concurrent decisions/preparation, expected-status CAS, callback retries and
immutable audit records. A real custom pg_dump archive is listed and restored
into a fresh test database; the complete durable fingerprint and accepted review
reference match afterward.

## Self-review findings addressed

- Serialized decisions/preparation on the proposal row, with ownership checked
  inside the transaction and a held ownership-row lock.
- Added expected-status CAS so competing defer/terminal operations cannot both
  overwrite the same observed pending state.
- Bound callbacks and accepted_review_id to immutable revisions; superseded
  acceptance callbacks conflict. Identical terminal retries remain idempotent.
- Protected review/decision records with DB mutation triggers and terminal
  proposals against mutation; added durable-state consistency checks.
- Hardened source reads with Linux descriptor-relative no-follow opens; rejected
  Windows junctions, aliases, traversal, hidden and nonportable paths.
- Made CR/tab/backslash differences visible and rejected invisible control
  characters. Full old/new bytes remain stored; the display diff is not an apply patch.
- Sent decision buttons only after complete bounded diff delivery. Legacy invalid
  targets allow Reject/Defer but never Accept; refresh is explicit.
- Included both new durable tables in backup, restore and fingerprint coverage.
- Removed stale documentation claims and typed the Telegram review boundary.

No remaining blocker was identified in this self-review. Independent external
review is still required before merge or a separately authorized rollout.

## Remaining limits and V3.3 obligations

- Accept stores consent only. V3.3 must revalidate the base and apply the exact
  stored new bytes atomically; a read-time source check is not a filesystem/DB lock.
- The configured knowledge root is trusted. Windows pre/post link checks are not
  a defense against an untrusted process actively mutating the mount concurrently.
- Paths use a conservative portable ASCII subset; each old/new text is limited
  to 128,000 UTF-8 bytes. Invalid legacy drafts need correction through the trusted
  service API; Telegram is not a Markdown editor.
- Telegram delivery remains at-least-once, so retries can duplicate preview
  messages. `/proposals` currently lists at most 100 open/deferred items.
- New durable tools require the migrated schema. Restore old archives with the
  matching old schema/tools before migrating; fingerprints are version-specific.
- No live Telegram/LLM E2E, production migration, server access, deployment or
  merge was performed. All external application calls in tests use fakes.

## Cleanup

Removed only the task-created PostgreSQL container after verifying its exact ID
and task label; its synthetic tmpfs test databases were discarded. No persistent
volumes were used or deleted. The temporary Linux test container removed itself.
No global Docker cleanup was run; unrelated containers and images were untouched.
