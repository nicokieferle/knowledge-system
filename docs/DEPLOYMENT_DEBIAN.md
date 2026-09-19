# Debian 12 Docker deployment

## V3.3 storage and migration gate (not deployed by this change)

V3.3 must not make the application checkout writable. Set `KNOWLEDGE_DATA_ROOT` to
the absolute host path of a separately persistent Knowledge tree; the documented
layout is:

```text
/path/to/Knowledge-System/                         application checkout, deployable/clean
/data/knowledgesystem/knowledge-repository/        operator-managed data checkout
└── knowledge/                                     KNOWLEDGE_DATA_ROOT, canonical Markdown
```

The parent may be a dedicated Git checkout, but the containers receive only its
`knowledge/` directory. The service has no `.git`, SSH key, token, commit or push
capability. Applying a revision intentionally makes that dedicated data checkout
dirty until an operator reviews and versions it; code deployments neither overwrite
it nor fail because the application checkout is dirty.

The image runs as UID/GID 10001. Before an authorized rollout, create/seed the data
tree from the currently deployed canonical `knowledge/`, verify the copy byte-for-byte,
then make the data tree and every writable parent/file owned by 10001:10001. The root
must be a real local directory, mode `0700` or another mode without group/other write.
Existing Markdown should be service-owned and not group/world writable. This is an
operator migration: do not rely on Compose to create the bind source with accidental
root ownership. Example commands must be adapted to the actual checkout and backup
policy:

```bash
sudo install -d -m 0700 -o 10001 -g 10001 \
  /data/knowledgesystem/knowledge-repository/knowledge
sudo rsync -a --delete --chown=10001:10001 \
  ./knowledge/ /data/knowledgesystem/knowledge-repository/knowledge/
diff -qr ./knowledge /data/knowledgesystem/knowledge-repository/knowledge
```

Do not use `--delete` after the initial, explicitly verified seed: it could erase
subsequently applied canonical changes. Put the absolute path in protected
`.env.server` as `KNOWLEDGE_DATA_ROOT`. `knowledge-mcp` and `telegram-bot` mount it
read-only. `review-web` alone mounts it read-write and also receives the existing
persistent Hugging Face cache plus the exact configured embedding model/dimensions.
Its container root remains read-only, non-root, capability-free and without a Docker
socket.

Before schema migration, stop only `review-web`, take both a durable PostgreSQL backup
and an independent filesystem snapshot/copy of the Knowledge data tree, then run the
new image's explicit `knowledge init-db`. The additive migration creates the eleventh
durable table, `proposal_applies`; it never creates an apply intent, writes Markdown or
indexes. Previously accepted proposals remain unapplied. Validate an isolated restore
and both backups before starting `review-web`. No rollout was performed by this task.
For an already initialized V3.3 database, the same transactional initialization adds and
validates the strengthened apply-result constraint requiring a non-NULL matching hash.
An inconsistent historical success row makes migration fail atomically; stop and investigate
it rather than editing or auto-repairing the audit record.

After rollout, a recoverable backup set consists of both artifacts:

- the eleven-table durable PostgreSQL archive; and
- a timestamped, access-restricted filesystem snapshot or operator-reviewed Git commit
  of `/data/knowledgesystem/knowledge-repository`.

Neither artifact alone represents the complete state. After recovery, compare each
applied journal hash with the restored file and rebuild/retry the disposable index as
needed. Never copy the application checkout over the data tree during an update.

Apply uses local-Linux `renameat2`, file/directory `fsync` and inode semantics. NFS or
other filesystems without equivalent guarantees are unsupported. A hard process,
container, runner, host or power failure can interrupt the web request; the durable
journal and apply-specific temp state make supported retries deterministic, but no
global PostgreSQL/filesystem/index transaction or hardware-level guarantee is claimed.
Exact-new recovery keeps the synchronized file descriptor open through parent `fsync`
and final inode validation. This closes exchanges during that recovery protocol, not
arbitrary hostile same-UID mutation after the entire operation has completed.

## V3.2.1 private review console (separate future rollout)

This change does not deploy anything. `review-web` is an independent, non-root,
read-only-root service. V3.3 changes only its Knowledge bind to read-write and gives it
the same persistent model cache required for document indexing; it still has no
Telegram/MCP dependency. It requires configured Argon2id/session secrets and an
explicit owner mapping; empty example secrets intentionally fail closed.

Host publication defaults to `127.0.0.1:8080` even when REVIEW_BIND_ADDRESS is empty.
Only the isolated container process listens on 0.0.0.0. Later WireGuard binding
requires an explicitly reviewed host address and private HTTPS termination; never
use a wildcard host binding. No firewall, proxy, DNS or WireGuard configuration is
changed here. Production cookies require HTTPS. Keep loopback `127.0.0.1:8080` in
REVIEW_ALLOWED_HOSTS for `/healthz`, which exposes only process readiness, not DB data.

See [setup, secrets, owner mapping and local tests](v321-browser-review.md).
V3.2.1 itself introduced no schema/backup changes. V3.3 extends protection to eleven
tables. Apply/index is request-driven and never part of startup.

## V3.2 durable review migration (not deployed by this change)

Before a separately authorized rollout, take a V3.1.2 durable backup using the old
checkout. Run the new `init-db` only under the deployment gate. Its transactional,
named migration replaces `proposals_status_check`, adds `accepted_review_id` and
creates `proposal_reviews` and `proposal_decisions` without deleting existing rows.
Repeated initialization preserves migrated data. Then take a V3.2 backup.

The V3.2 durable allowlist contained ten tables. Both review tables and the accepted
revision reference are included in backup/restore/fingerprint. Review rows contain
complete private old/new content and diffs: protect them like conversation history.
The accepted-review FK introduces a cycle with proposals; only that foreign key is
`DEFERRABLE INITIALLY DEFERRED`. The fresh-target restore keeps all foreign keys and
immutability triggers enabled, and loads data, repairs the message sequence and checks
integrity in one transaction. It is never a restore into the live database. No new identity sequence
is introduced (review/decision IDs are UUIDs).

Restore a V3.2 archive only into an initialized V3.2 schema. For a V3.1.2 archive,
restore using the V3.1.2 schema/tools in a fresh database, then migrate; do not use
cross-version fingerprints as equivalent. New allowlists must not be run against
an unmigrated live database. No PostgreSQL volume recreation, index run or knowledge
write is required for V3.2. MCP remains read-only with the same two tools.

This deployment keeps `knowledge/` as the canonical knowledge source. PostgreSQL contains a
disposable, rebuildable retrieval index and, from V3.0 onward, may also contain durable
conversation and proposal state. It exposes the MCP endpoint only on the Debian host loopback
interface. It does not provide TLS, authentication or public network access.

## Architecture

```text
Debian application Git checkout (never written by the service)

Dedicated persistent Knowledge tree
  knowledge/ (canonical Markdown; review-web rw, MCP/Telegram ro)
        |
        v
knowledge-mcp container (non-root, CPU, Streamable HTTP on 0.0.0.0:8000)
        |
        +--> PostgreSQL + pgvector container
        |      `--> knowledge_pgdata volume
        |
        `--> knowledge_hf_cache volume

Debian host publishes only 127.0.0.1:8000 -> container port 8000
```

`compose.server.yml` is intentionally separate from `docker-compose.yml`. The existing file
remains the small PostgreSQL-only configuration used by local Windows development.

## Prerequisites

- Debian 12 with current security updates
- Docker Engine and the Docker Compose plugin
- Git
- enough disk space for the container image, PostgreSQL index and roughly 2 GB reranker cache
- enough RAM to run PostgreSQL and the CPU reranker concurrently

Verify the installation:

```bash
docker version
docker compose version
git --version
```

## First installation

Clone the repository and create a server-only environment file:

```bash
git clone https://github.com/Hengsto/Knowledge-System.git
cd Knowledge-System
cp .env.server.example .env.server
chmod 600 .env.server
```

Replace the example database password in both `POSTGRES_PASSWORD` and `DATABASE_URL`.
`openssl rand -hex 32` produces a URL-safe value. Do not commit `.env.server`; it is ignored
by Git and excluded from the Docker build context.

Also set `KNOWLEDGE_DATA_ROOT` to the pre-created absolute persistent data path from
the V3.3 storage gate above. Compose deliberately fails interpolation when it is absent.

Build the CPU image and start PostgreSQL:

```bash
docker compose --env-file .env.server -f compose.server.yml build knowledge-mcp
docker compose --env-file .env.server -f compose.server.yml up -d postgres
docker compose --env-file .env.server -f compose.server.yml ps
```

Initialize and build the disposable retrieval index explicitly:

```bash
docker compose --env-file .env.server -f compose.server.yml run --rm knowledge-mcp knowledge init-db
docker compose --env-file .env.server -f compose.server.yml run --rm knowledge-mcp knowledge index
```

These commands are deliberately not part of container startup. Starting the MCP service
does not initialize the database, rebuild the index, modify Markdown or run Git.

Start the MCP service:

```bash
docker compose --env-file .env.server -f compose.server.yml up -d knowledge-mcp
docker compose --env-file .env.server -f compose.server.yml ps
docker compose --env-file .env.server -f compose.server.yml logs --tail=100 knowledge-mcp
```

The endpoint is now available only on the Debian host:

```text
http://127.0.0.1:8000/mcp
```

## Verify binding and MCP

Confirm Docker publishes only the loopback address:

```bash
docker compose --env-file .env.server -f compose.server.yml port knowledge-mcp 8000
ss -lnt | grep ':8000'
```

The first command must report `127.0.0.1:8000`, not `0.0.0.0:8000`. A connection to the
server's public interface on port 8000 must fail. The exact external-interface test depends
on the host firewall and available second machine; do not add a public firewall rule for it.

Run the standard MCP v2 client contained in the built image against the host endpoint:

```bash
docker run --rm --network host knowledge-system-mcp:local \
  python /app/scripts/mcp_http_smoke.py http://127.0.0.1:8000/mcp
```

The smoke test performs `initialize`, requires exactly `search_knowledge` and `get_document`,
runs one fast search, runs two quality searches in the same MCP process, and fetches the
canonical original document for the top quality result. It reports timings and metadata but
does not print document contents.

The first quality request can take substantially longer while
`BAAI/bge-reranker-v2-m3` is downloaded and loaded. The second request reuses the in-process
reranker. `HF_HOME` points at the persistent `knowledge_hf_cache` volume, so later container
processes reuse the downloaded model files even though they load the model into RAM again.

Inspect the cache without printing its contents:

```bash
docker compose --env-file .env.server -f compose.server.yml exec knowledge-mcp \
  sh -lc 'du -sh "$HF_HOME"'
docker volume inspect knowledge-system_knowledge_hf_cache
```

## Restart and persistence

Record the current chunk count and cache size:

```bash
docker compose --env-file .env.server -f compose.server.yml exec postgres \
  sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT count(*) FROM chunks"'
docker compose --env-file .env.server -f compose.server.yml exec knowledge-mcp \
  sh -lc 'du -sh "$HF_HOME"'
```

Restart the service and rerun the MCP smoke test:

```bash
docker compose --env-file .env.server -f compose.server.yml restart knowledge-mcp
docker compose --env-file .env.server -f compose.server.yml ps
docker run --rm --network host knowledge-system-mcp:local \
  python /app/scripts/mcp_http_smoke.py http://127.0.0.1:8000/mcp
```

The PostgreSQL chunk count must be unchanged, `knowledge/` remains available through its
host bind mount, and the Hugging Face cache size remains populated. The new MCP process loads
the reranker into RAM again from cached files.

Verify persistence across a normal Compose shutdown:

```bash
docker compose --env-file .env.server -f compose.server.yml down
docker compose --env-file .env.server -f compose.server.yml up -d
docker compose --env-file .env.server -f compose.server.yml ps
```

Named volumes survive `down`. Do not use `docker compose ... down -v` after V3 conversation
features hold real data: it deletes the PostgreSQL volume, including non-rebuildable
conversations, messages, summaries, suggestions and proposals. Back up that volume before
destructive maintenance. V3.1 client routing state is also non-rebuildable and belongs in
the same durable backup. Canonical Markdown remains in the Git checkout, while only the
retrieval tables and model cache can be recreated from source.

## Durable state backup

PostgreSQL contains two operationally different data classes:

- Rebuildable: `chunks`, retrieval indexes and `index_metadata`.
- Non-rebuildable: `conversations`, `messages`, `conversation_summaries`,
  `proposal_suggestions`, `proposals`, `client_states`, `client_conversations` and
  `client_message_bindings`, `proposal_reviews` and `proposal_decisions`.

The scripts use the PostgreSQL 17 `pg_dump` and `pg_restore` binaries already present in the
PostgreSQL container. They never put the database password on the command line. The durable
table allowlist is maintained once in `scripts/durable_tables.sh`.

Create the host backup directory with restrictive permissions. The operator running Docker
must be able to write there:

```bash
sudo install -d -m 0700 -o "$USER" -g "$USER" /data/knowledgesystem/backups
```

Create a timestamped custom-format, data-only archive:

```bash
BACKUP_DIR=/data/knowledgesystem/backups \
  bash scripts/backup_durable_state.sh
```

The script uses `.env.server` and `compose.server.yml` by default, writes through a restrictive
temporary file, checks that `pg_dump` succeeded, verifies that `pg_restore --list` can read the
archive, and prints `backup_file`, `backup_bytes`, `backup_format` and
`backup_archive_check=pass`. It refuses `/data/knowledgesystem/postgres` as a backup target.

Inspect an archive without restoring or printing conversation contents:

```bash
BACKUP=/data/knowledgesystem/backups/durable-state-YYYYMMDDTHHMMSSZ-ID.dump
docker compose --env-file .env.server -f compose.server.yml exec -T postgres \
  pg_restore --list < "$BACKUP"
```

This is only a fast archive-readability check. PostgreSQL can still warn about the intentional
`proposals`/`proposal_reviews` data-only dependency cycle, and the warning is not suppressed.
Only a real restore into an initialized fresh database, followed by the fingerprint and
protection checks below, demonstrates recoverability.

Archives contain private conversation and proposal data. Keep the directory non-public,
restrict copies to trusted operators and never commit `.dump` files.

### Restore rehearsal in a fresh database

V3.0 deliberately does not restore directly into the configured production database. Create
a separate database in the same PostgreSQL instance, initialize its schema and then restore:

```bash
export RESTORE_DB="knowledge_restore_$(date -u +%Y%m%dT%H%M%SZ)"

docker compose --env-file .env.server -f compose.server.yml exec -T postgres \
  sh -eu -c 'exec createdb --username="$POSTGRES_USER" "$1"' \
  create-restore-db "$RESTORE_DB"

docker compose --env-file .env.server -f compose.server.yml run --rm knowledge-mcp \
  sh -eu -c 'export DATABASE_URL="${DATABASE_URL%/*}/$1"; exec knowledge init-db' \
  restore-init "$RESTORE_DB"

RESTORE_DATABASE="$RESTORE_DB" \
  bash scripts/restore_durable_state.sh "$BACKUP"
```

The restore script requires all durable tables to exist and contain zero rows. It refuses the
configured production database and any non-empty target. It does not use `--clean`, `DROP`,
`TRUNCATE` or `--disable-triggers`. It extracts only the allowlisted table data to a mode-0600
temporary SQL file inside the PostgreSQL container, then executes that SQL, the message-sequence
repair and the durable-integrity guard in one `psql --single-transaction` operation with
`ON_ERROR_STOP`. The temporary SQL file is removed on success, error or interruption. The
deferred accepted-review foreign key is checked at commit; all other foreign keys remain
immediate and all immutability triggers stay enabled. Any load, sequence, integrity or commit
failure rolls back the whole restore. A second invocation against the restored database must
fail without changing data.

Compare source and restored state without printing message, summary or proposal text:

```bash
docker compose --env-file .env.server -f compose.server.yml run --rm knowledge-mcp \
  python /app/scripts/durable_state_fingerprint.py

docker compose --env-file .env.server -f compose.server.yml run --rm knowledge-mcp \
  sh -eu -c 'export DATABASE_URL="${DATABASE_URL%/*}/$1"; \
    exec python /app/scripts/durable_state_fingerprint.py' \
  restore-verify "$RESTORE_DB"
```

Both commands must report identical counts and `durable_state_sha256`. The fingerprint covers
IDs, statuses, foreign-key relationships, summary boundaries, suggestion links, client routing
links, review revisions, decisions, apply/index journal and hashed private fields. It never
prints the private field values. Verify all eleven
table counts, the fingerprint, a successful reconnect and the refusal of a second restore
before considering a backup recoverable.

## Isolated V3.0 PostgreSQL recovery smoke

The repeatable smoke uses `pgvector/pgvector:0.8.6-pg17-bookworm`, a unique Compose project,
one project-scoped PostgreSQL volume, no published ports and a read-only `knowledge/` mount.
It tests real persistence, summary compare-and-set, proposal idempotency, concurrent suggestion
confirmation, backup, restore into a second database and defensive refusal. It never references
`/data/knowledgesystem/postgres` or the production Compose project.

Run it from an isolated checkout or Git worktree:

```bash
bash scripts/run_v30_postgres_recovery_smoke.sh
```

The script generates an in-memory random test password, builds a feature-branch image, removes
only its explicitly named Compose project, volume, image and `/tmp/knowledge-v30-smoke.*`
directory on exit, and reports whether production resources were touched. Do not replace its
project-name and path guards with broad Docker cleanup commands.

For an already provisioned isolated database, the pytest wrapper is opt-in:

```bash
TEST_DATABASE_URL='postgresql://test-user:test-password@test-host/knowledge_v30_test' \
V30_SMOKE_CONFIRM=isolated-v30-smoke-database \
pytest tests/integration/test_conversation_postgres.py -q
```

Without `TEST_DATABASE_URL`, normal `pytest` skips this one real-database test.

### Verified target-host recovery rehearsal

The full V3.0 lifecycle was verified on the Debian target host on 2026-08-31 from an
isolated worktree at commit `5b53413`. The test used a separate Compose project, an
unpublished PostgreSQL 17 + pgvector container and a project-scoped volume. It confirmed:

- durable conversation, message, summary, suggestion and proposal persistence;
- atomic and idempotent suggestion confirmation plus concurrent update handling;
- a custom-format backup containing only the durable tables and message sequence;
- an identical source/restore fingerprint after restore into a fresh second database;
- refusal of a repeated restore into the non-empty target without changing its fingerprint;
- identical source/restore message sequence state; and
- continued health of the production MCP and PostgreSQL containers throughout the test.

The production checkout remained on `main`, `/data/knowledgesystem/postgres` was not used by
the smoke project, and the test container, network, volume, image, backup, secret file and
worktree were removed after verification.

## Logs and stop

```bash
docker compose --env-file .env.server -f compose.server.yml logs -f knowledge-mcp
docker compose --env-file .env.server -f compose.server.yml stop
```

Application logs contain lifecycle and error diagnostics. The deployment does not add query
or full-document logging.

## Updating

For operator-authored Knowledge changes, work only in the dedicated data checkout, not
the application checkout. First inspect and version or otherwise back up any browser-applied
dirty changes. Never pull/reset over an uncommitted apply. After the operator has produced
the intended clean data-tree revision, reindex explicitly without rebuilding the image:

```bash
git -C /data/knowledgesystem/knowledge-repository status --short
# Operator-controlled commit/fetch/fast-forward only after reviewing the status above.
docker compose --env-file .env.server -f compose.server.yml run --rm knowledge-mcp knowledge index
```

When application code or dependencies changed, rebuild and recreate the service before
reindexing:

```bash
git pull --ff-only
docker compose --env-file .env.server -f compose.server.yml build knowledge-mcp
docker compose --env-file .env.server -f compose.server.yml up -d
docker compose --env-file .env.server -f compose.server.yml run --rm knowledge-mcp knowledge index
docker compose --env-file .env.server -f compose.server.yml ps
```

No Git synchronization, database initialization or indexing runs automatically.
# V3.1 Telegram service (manual deployment)

V3.1 adds a `telegram-bot` long-polling service without an inbound port or Cloudflare/DNS
change. It imports `ConversationService` and `KnowledgeService` directly; it does not call
the local MCP endpoint. Use a dedicated Knowledge-System bot token, never the Journaling
bot token.

After backing up durable state using the existing procedure, deploy manually:

```bash
cd /path/to/Knowledge-System
git fetch origin
git checkout codex/v31-telegram-chat-routing
git pull --ff-only
cp .env.server .env.server.v30.backup
# Add LLM_PROVIDER, LLM_MODEL, LLM_API_KEY, optional LLM_BASE_URL and
# LLM_TIMEOUT_SECONDS, plus TELEGRAM_BOT_TOKEN to .env.server.
docker compose --env-file .env.server -f compose.server.yml config --quiet
docker compose --env-file .env.server -f compose.server.yml build
docker compose --env-file .env.server -f compose.server.yml run --rm knowledge-mcp knowledge init-db
docker compose --env-file .env.server -f compose.server.yml up -d postgres knowledge-mcp telegram-bot
docker compose --env-file .env.server -f compose.server.yml ps
docker compose --env-file .env.server -f compose.server.yml logs --tail=100 telegram-bot
```

Smoke-test in the dedicated Telegram bot: run `/new Knowledge-System`, send a normal
question, run `/new Investments`, send an investment question, inspect `/topics`, switch
back with `/switch 2` or the displayed UUID, then use `/remember` and exercise both inline
suggestion choices. Restart with
`docker compose --env-file .env.server -f compose.server.yml restart telegram-bot` and
verify `/topics` and the active topic persisted. Finally verify MCP still advertises only
`search_knowledge` and `get_document` using the existing MCP smoke procedure.
