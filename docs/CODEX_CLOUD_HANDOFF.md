# Codex Cloud handoff

This document records the verified transition state after V3.0 and defines the next task for
Codex Cloud. It contains no credentials or private key material.

## Repository state

- GitHub `main` is at `a35397abe60b89ed52ea68771314f8be71d3451f` (`Add V3.0
  conversation and memory core (#1)`).
- V3.0 has passed the local suite (`110 passed, 1 skipped`) and an isolated PostgreSQL 17
  backup/restore rehearsal on the Debian target host.
- The one skipped test is the opt-in real PostgreSQL integration test; the equivalent target
  host smoke completed successfully.
- The local Windows virtual environment later became unusable because its Python interpreter
  path disappeared. Codex Cloud must install `.[dev]` into a clean environment and rerun the
  complete suite instead of relying on that environment.

## Production state at handoff

The following state was observed through the restricted project status command and the
operator's root session. Treat it as historical input and verify it again before deployment.

- Checkout: `/srv/projects/knowledge-system`
- Production branch/commit: clean `main` at
  `a5c594fec0be1d9b940739173420482148ccda7e`
- Remote `origin/main`: `a35397abe60b89ed52ea68771314f8be71d3451f`
- The update is a fast-forward.
- `knowledge-system-knowledge-mcp-1` and `knowledge-system-postgres-1` were healthy.
- PostgreSQL and the model cache remain at `/data/knowledgesystem/postgres` and
  `/data/knowledgesystem/huggingface`.
- A validated full pre-V3 PostgreSQL backup was created under the root-only directory
  `/data/knowledgesystem/backups/pre-v30-20260901T174454Z`.
- `.env.server` is root-owned with mode `0600`; `/data/knowledgesystem/backups` is root-owned
  with mode `0700`.
- The production checkout, database and MCP service have not been updated to V3.0 yet.

## Existing restricted server control

The server already has a restricted `codex-deploy` account. Do not create SSH keys, request a
private key or passphrase, copy local credentials into Codex Cloud, or redesign SSH access.
Codex Cloud must not assume it can reach the private Debian host.

The root-owned server components are:

- `/usr/local/sbin/codex-ssh-dispatch`
- `/usr/local/sbin/codex-project-control`
- `/etc/codex-deploy/projects.d/knowledge-system.conf`
- `/etc/sudoers.d/codex-deploy-control`

The account is not in the Docker group. The dispatcher currently permits only:

```text
project-list
project-status knowledge-system
```

The control scripts were backed up by the operator under
`/root/codex-deploy-control-pre-project-deploy-20260901T194441Z`. This path is not accessible
to `codex-deploy`.

## Next objective

Version and test the proposed deployment control as repository artifacts. Do not install it
on the server, deploy a commit, merge a pull request, or modify production from Codex Cloud.
The operator will install reviewed root-owned files manually through the existing PuTTY root
session.

The intended remote command is exactly:

```text
project-deploy knowledge-system <full-lowercase-40-character-commit-sha>
```

Implement templates under an explicit operations directory such as `ops/codex-deploy/`, with
installation documentation and tests. Preserve the current `project-list` and
`project-status` behavior.

## Required security properties

1. The SSH dispatcher accepts exactly one registered project and one lowercase 40-character
   hexadecimal commit SHA. Extra arguments, shell metacharacters and unknown actions fail.
2. `codex-deploy` remains outside the Docker group and receives no direct sudo permission for
   Docker, Git, Compose, shells, editors, file-copy tools or network administration.
3. The existing root-owned control script remains the only sudo entry point. It closes stdin,
   uses a fixed `PATH`, removes caller-controlled Git/Docker/Compose environment variables and
   never uses `eval`.
4. Repository, remote URL, branch, Compose file, environment file, service names, MCP smoke
   URL and backup root are fixed by root-owned configuration and strictly validated.
5. Deployment requires a root-owned mode-`0600` one-time approval record containing exactly
   the requested SHA. Acquire a per-project lock and consume the approval atomically before
   the first mutation. A failure or retry requires fresh operator approval.
6. Require a clean `main`, exact remote URL, `target == origin/main`, a valid commit object and
   a fast-forward from the deployed commit. Disable repository hooks for privileged Git
   operations and prohibit interactive credential prompts.
7. Before changing the checkout, create and validate a mode-`0600` custom-format PostgreSQL
   backup outside `/data/knowledgesystem/postgres`. Never print backup contents or secrets.
8. Build the new MCP image while the existing MCP process remains available. Run additive
   `knowledge init-db`, then incremental `knowledge index`, recreate only `knowledge-mcp`, and
   verify container health plus the existing MCP HTTP smoke. Do not restart PostgreSQL.
9. Preserve `/data/knowledgesystem/postgres`, `/data/knowledgesystem/huggingface`, canonical
   `knowledge/`, `.env.server`, durable conversation tables and existing retrieval chunks.
10. Define and test failure behavior. The old MCP container must remain running until the new
    image and additive database initialization succeed. Any automatic rollback must operate
    only after verifying the original checkout was clean and must never restore over the
    production database.

## Required tests and review

- Dispatcher accepts the exact deploy grammar and rejects malformed commands.
- Control rejects unknown projects, invalid SHAs, extra arguments and unapproved deployments.
- The one-time approval cannot be reused, including after failure or concurrent invocation.
- Root-owned path, mode, symlink and canonical-path checks are covered.
- No mutation command runs before lock acquisition, approval consumption and backup success.
- Compose/Docker/Git commands use fixed arguments and sanitized environments.
- Existing `project-list` and `project-status` tests remain unchanged and pass.
- Run `pytest`, `ruff check .`, `git diff --check`, and shell syntax checks.
- Review the complete diff before committing to a `codex/` branch and opening a PR.

## Manual acceptance after merge

Only after review and explicit operator approval:

1. Back up the current root-owned control files again.
2. Install the reviewed files with explicit root ownership and restrictive modes.
3. Run `sh -n`/`bash -n`, `visudo -cf`, and read-only dispatcher regression checks.
4. Verify a deploy without a root approval record is denied before Git, Docker or DB changes.
5. Create a one-time approval for the exact merged SHA from the PuTTY root session.
6. Invoke `project-deploy knowledge-system <sha>` through the existing restricted account.
7. Confirm clean server `main` at that SHA, unchanged PostgreSQL container identity, healthy
   services, durable V3.0 tables, preserved chunk count and successful MCP smoke.

Do not start V3.1 client or LLM work until this controlled V3.0 production update has been
completed or consciously deferred by the operator.

## Prompt for the next Codex Cloud task

```text
Work in Hengsto/Knowledge-System from the latest main. Read AGENTS.md, README.md,
docs/ROADMAP.md, docs/DEPLOYMENT_DEBIAN.md and docs/CODEX_CLOUD_HANDOFF.md completely.

Implement only the versioned, strongly restricted project-deploy control described in the
handoff. Add reviewed templates under ops/codex-deploy plus tests and operator installation
documentation. Preserve project-list and project-status. Do not access or modify the Debian
server, create SSH credentials, expose Docker directly, deploy any commit, merge the PR, or
start V3.1. Run pytest, ruff check ., git diff --check and shell syntax checks. Commit on a new
codex/ branch, push it, and report the exact diff and remaining risks for manual review.
```
