# V3.3 validation and security review

## Scope and baseline

- Baseline and current `origin/main`: `a19b1c5a1022f39bc9eeddcd612b687195b4f75c`.
- Development branch: `codex/v33-atomic-apply`.
- No production connection, deployment, Telegram/LLM call, merge or canonical Knowledge
  mutation was performed.
- All filesystem tests used pytest temporary roots. `knowledge/` remained byte/Git clean.

The storage gate found that the old server Compose bind (`./knowledge:/app/knowledge:ro`)
coupled canonical data to the application deployment checkout. V3.3 instead requires the
explicit persistent `KNOWLEDGE_DATA_ROOT`; MCP/Telegram receive read-only mounts and only the
private review service receives read-write access. Production cutover/seed/ownership and
independent filesystem backup remain operator prerequisites and were not executed.

## Local environment and commands

The final local gate used Python 3.13, rootless Docker, PostgreSQL 17.11 and pgvector 0.8.6.
It completed 393 unit/HTTP tests and 64 real PostgreSQL integration tests, with no failures
or skips in either separately executed group. The only warning was the upstream Starlette
`BlockingPortal` deprecation noted below. The final rootless BuildKit image build and the
built-image database smoke test also succeeded; the smoke test reported
`knowledge_unchanged=true` and all eleven durable tables, including an empty
`proposal_applies` in its clean smoke database. The commands were:

```bash
python -m pip check
python -m ruff check .
python -m ruff format --check .
python -m compileall -q src tests
python -m pytest tests --ignore=tests/integration -q -ra
python -m pytest tests/integration -q -ra
git diff --check
docker compose --env-file .env.server.example -f compose.server.yml config --quiet
CI_IMAGE=knowledge-system-v33:local docker compose --env-file /dev/null \
  -f compose.ci.yml config --quiet
docker buildx build --builder rootless --load -t knowledge-system-v33:local .
```

The Compose review check supplies synthetic non-production values for required review secrets
only to validate interpolation. Integration URLs point exclusively at the loopback-published
ephemeral PostgreSQL test container. Test doubles replace embeddings where the real model is
irrelevant; the built container smoke makes no LLM or Telegram request.

## GitHub acceptance

The first pushed code head, `f643b475720313fd546cd82bebb0a1d0df52b337`, passed
[GitHub Actions run 35298223594](https://github.com/Hengsto/Knowledge-System/actions/runs/35298223594).
`CI / admission` and `CI / verify` both succeeded. GitHub reported runner `ci` with labels
`self-hosted`, `linux`, `x64`, `ci`, `rootless-docker`, `knowledge-system`; preflight confirmed
Python 3.13.5, the rootless daemon and the Docker Buildx `docker` driver. The job token exposed
only `contents: read`, checkout persisted no credentials, and installation used a newly created
virtual environment.

The exact pip cache key was a hit, while dependency installation and every check still ran.
BuildKit reused eligible base layers and loaded the resulting image for the successful smoke
test. The two JUnit files contain 389 and 54 tests respectively, with zero failures, errors or
skips. Project-scoped cleanup removed the run's container, volume, network and image before all
cache/checkout post-steps succeeded. The seven-file artifact
`ci-results-35298223594-1` contains both JUnit reports, dependency inventory, builder/build/smoke
logs and service diagnostics; its GitHub-recorded SHA-256 is
`873181c17e1139695657059ab50e649f3cc28439c6ab0caa76d98717ae66e438`.

The service log's PostgreSQL `ERROR` records are the deliberate rollback, constraint and
mid-restore injections asserted by successful tests, not unhandled CI failures. The only test
warning remains the upstream Starlette deprecation; the image build also emits pip's expected
root-user warning inside the disposable Docker build stage.

## Security and correctness evidence

The independent PR review's two high and four medium findings were reproduced against
`374af059d80258033634404169c51e4945016ba5` before remediation. Regression coverage now proves
that exact-new retries repeat file and directory synchronization, full indexing cannot overlap
apply snapshots, case aliases share one document lock, conflict successors retain the accepted
human target/content, browser recovery accepts both pending and failed index states, and
`applied` rows require a non-NULL matching hash. The local counts above come from the completed
validation artifacts; each pushed head is additionally accepted by the repository workflow.

### Domain, schema and durable state

- Real PostgreSQL error injection proves the V3.3 `DO` migration rolls back completely even
  with `autocommit=True`.
- A failure while inserting the intent rolls back the accepted decision and proposal status;
  no file operation starts.
- Old pending/rejected/deferred/accepted rows retain their V3.2 meaning. In particular, old
  accepted rows have no apply row and require explicit browser Apply.
- Constraints/triggers reject mismatched review bindings, malformed success records, mutation
  of successful apply/index results and deletion of the apply audit row.
- Existing valid V3.3 schemas receive the named apply-result constraint idempotently. A corrupt
  NULL-hash success or an injected later DDL failure rolls the entire migration back without
  changing the row or leaving a partial constraint.
- The real archive roundtrip includes a nonempty `proposal_applies`, all eleven durable tables,
  exact fingerprint equality, validated FKs, enabled triggers and repaired sequence state.
  Existing corrupt-archive, incompatible-schema, nonempty-target and injected mid-restore
  rollback cases remain active.

### Source and atomic file apply

- Existing/update and absent/create paths verify exact bytes, hashes and mode handling.
- Traversal, absolute paths, missing/non-directory parents, target/parent symlinks, root aliases,
  file/directory case aliases, non-regular targets and oversized/unstable reads fail closed.
- Device/inode/type/size/`mtime`/`ctime` participate in stable metadata; `atime` is deliberately
  excluded and covered by the existing Linux atime regression.
- Deterministic hooks cover target and parent exchange, target appearance, temp-name collision,
  partial writes, write/file-`fsync`/pre-rename errors, post-exchange process loss, directory
  `fsync` boundary, exact-new idempotency and apply-specific orphan cleanup.
- Create and update retries after the rename repeat target-file and parent-directory `fsync`;
  persistent failure remains nonterminal and a later successful retry heals it.
- A third target state survives; tests never write the repository's real `knowledge/`.

### Concurrency, crash recovery and indexing

- Two simultaneous submits for one revision produce one decision, one journal and one index
  call. Two accepted proposals for one base file serialize to one applied/one conflict result.
- Both deterministic start orders for `note.md`/`Note.md` yield exactly one file and one conflict.
  The lock identity is the casefolded form of the existing validated portable path namespace.
- Full indexing holds an exclusive global advisory lock from before discovery through its index
  commit. Apply/document indexing take the shared global lock before the canonical path lock;
  controlled two-document interleaving finishes without mixed revisions, false `indexed`, or
  deadlock, and retry remains idempotent.
- Refresh during Apply waits for the row lock and cannot replace a successful result. Two
  simultaneous conflict refreshes reuse one successor proposal/revision; changed human target
  and candidate bytes survive both parallel and repeated conflict refreshes.
- A crash after rename but before DB completion leaves the journal pending; retry recognizes
  the exact new file and confirms success. A committed index followed by lost status
  confirmation is replaced idempotently on retry with no duplicate chunks.
- Document replacement deletes/inserts only one source/path in one PostgreSQL transaction. A
  real injected SQL error after deletion rolls back to the complete old chunk set.
- Index failure keeps the applied file, stores only a controlled class and succeeds through the
  separate retry.

### Browser, Telegram and MCP boundaries

- Auth/session rotation, CSRF on every POST, backend ownership, safe redirects, output escaping,
  bounded forms and duplicate/superseded submits remain covered.
- Mutating V3.3 routes reject GET. Browser-to-real-PostgreSQL tests cover Accept & Apply, legacy
  explicit Apply, conflict display/refresh successor, apply retry state, index failure/retry and
  safe error rendering.
- Browser-to-real-PostgreSQL recovery also covers a crash before index start and after index
  commit but before journal confirmation. `pending` and `failed` show the same POST-only retry;
  authentication, CSRF, ownership and apply-state checks remain active.
- Legacy Telegram review/decision/refresh/apply callback prefixes remain informational before
  domain lookup. The only actionable Telegram choice is proposal creation/rejection; no
  decision, apply or index-retry capability was added.
- MCP code and its two read-only tool contract are unchanged.

## Known limits and production prerequisites

- There is no global transaction across PostgreSQL, filesystem and index. The journal and
  retries expose/reconcile the supported intermediate states instead.
- Hard kernel/host/power loss can exceed application cleanup guarantees. Local filesystem and
  storage hardware must honor `renameat2` and `fsync`; network filesystems are unsupported.
- Same-UID processes are trusted not to perform hostile parent-directory renames. Production
  mode rejects a root not owned by the service UID or writable by group/other.
- The database archive does not back up Markdown. Production needs a coordinated independent
  filesystem snapshot or operator-reviewed Git workflow for the dedicated data tree.
- The application never commits/pushes Git. An operator must review/version dirty data and must
  never reset or deploy-copy over it.
- Production storage seed, ownership, backup rehearsal, migration and rollout remain manual;
  this branch does not contact or change production.
- The Starlette test client currently emits one upstream `BlockingPortal` deprecation warning;
  it causes no skip or failure. The unchanged workflow also passes local `actionlint`; the
  repository's actual GitHub CI remains the authoritative hosted execution after each push.
