# V3.3 — safe accepted-revision apply

## Technical baseline and storage gate

V3.2.1 already provided immutable review revisions containing exact old/new text,
SHA-256 values and reproducible diffs. `accepted_review_id` binds the accepted revision;
review/decision rows and terminal proposals are database-protected. Source snapshots use
portable path rules and POSIX descriptor-relative no-follow reads. The existing indexer was
global, and the durable allowlist contained ten tables.

Before V3.3, production Compose mounted `./knowledge` from the application Git checkout
read-only into every service. Making that mount writable would deliberately dirty the
deployment checkout and a later code deployment could overwrite knowledge or fail because
of local changes. The repository contained no contract for automatic service-side Git commits,
credentials or pushes. V3.3 therefore does not write that checkout.

`KNOWLEDGE_DATA_ROOT` now names a separately managed persistent host data tree. The intended
production value is `/data/knowledgesystem/knowledge-repository/knowledge`: its parent may be
an operator-managed dedicated Git checkout, while the containers see only the `knowledge`
subdirectory and no `.git` or credentials. MCP and Telegram mount it read-only; only
`review-web` mounts it read-write. Application image/root filesystems remain read-only.

The container runs as UID/GID 10001. Production startup requires a real, existing root (not a
symlink/alias), owned by the service UID, readable/writable/searchable by it and not writable
by group/other. Existing files and directories must likewise be operator-maintained as UID
10001; replacements preserve the existing permission bits, while new files use `0640`.
The host data tree survives image/checkout replacement. It needs its own filesystem backup or
operator Git commit workflow in addition to the PostgreSQL durable-state archive. No migration,
copy, Git command, index run or production write happens automatically.

## Persistent model

`proposal_applies` is the eleventh durable table. One row uniquely binds one proposal and one
accepted review revision. It copies the immutable logical target, expected old hash or explicit
absence, expected new hash and actor identity. It records:

- apply state: `pending`, `applied`, `conflict` or `failed`;
- index state: `pending`, `indexed` or `failed`;
- actual applied hash, safe classified errors, attempt counters and phase timestamps; and
- an optional uniquely bound successor proposal created by conflict refresh.

Foreign keys, unique/check constraints and triggers enforce accepted-revision equality,
content/hash binding, successful-state consistency, immutable bindings, terminal successful
results and append-only deletion protection. Accept & Apply writes the accepted decision,
proposal status and apply intent in one PostgreSQL transaction. A migration only installs
DDL; old accepted proposals receive no apply row and no file/index side effect.

If a conflict needs a new basis, the original accepted proposal cannot be reopened without
violating its immutable decision. `Refresh review` instead locks the conflict, appends one
generic control message and one pending successor proposal in the same transaction, binds it
to the conflict, and prepares the successor through the existing safe review snapshot. Retries
reuse the same successor. No historical decision or accepted binding changes.

## Filesystem procedure

Only Linux/POSIX apply is supported. For every attempt the writer validates the logical source
and portable relative Markdown path, opens the real root and each existing parent with
`O_DIRECTORY|O_NOFOLLOW`, rejects case aliases and pins the final parent descriptor. It reads
the target with `O_NOFOLLOW`, verifies regular-file type, size limit, device, inode, size,
`mtime`, `ctime`, stable path entry and complete bytes; `atime` is deliberately ignored.
The configured parent chain is checked again before the atomic operation and after durability.

The accepted text is encoded as exact UTF-8 and checked against its stored hash. A random,
apply-ID-scoped temporary file is created in the same directory with
`O_CREAT|O_EXCL|O_NOFOLLOW`. All bytes are written in a loop, file contents and permissions
are `fsync`ed, and existing mode bits are preserved when replacing. Creation uses Linux
`renameat2(RENAME_NOREPLACE)`. Replacement uses `RENAME_EXCHANGE`: this prevents a third state
that races after the last read from being destroyed. The displaced entry is byte/metadata
checked before removal. Finally the parent directory is `fsync`ed and the resulting target is
securely reread. Only regular temporary files with the exact apply-specific prefix are ever
considered for recovery; ambiguous states are preserved as conflicts.

No directory is created implicitly, no deletion operation exists, no process-local file lock
is treated as sufficient, and no path outside the configured root is intentionally opened for
writing. The production ownership/mode boundary prevents untrusted OS users from renaming the
parent tree. A process with the same UID can still race kernel path operations; post-checks
detect the supported exchanges, but same-UID hostile filesystem mutation is outside the trust
model.

## Crash consistency and retries

PostgreSQL, filesystem and index transactions cannot be globally atomic. The durable phases
make the actual state explicit:

| Failure point | Durable/file state | Retry behavior |
|---|---|---|
| before intent commit | no accepted decision/intent from that request; no write | submit again |
| after intent, before write | `pending`, old/absent target | Retry apply |
| temp write/file `fsync`/before rename | failed/pending, original target; own temp removed | Retry apply |
| after exchange, before directory `fsync` | journal may be `pending`; target and scoped displaced temp identify the operation | recover/finalize or preserve conflict |
| after durable rename, before DB confirmation | `pending`, target has exact new hash | idempotently mark applied |
| before/during index | file remains applied; index pending/failed | Retry indexing |
| after index commit, before status confirmation | complete new chunks, status pending | repeat document replacement and confirm |

A target already containing the exact new bytes is accepted only for this apply row's exact
bound revision. An old/absent target proceeds. Every other state is a conflict and is never
overwritten. Apply failures never trigger indexing. Index failures never roll back or hide the
applied file.

## Concurrency and indexing

Proposal/apply rows are locked in PostgreSQL. Apply and indexing also acquire the same stable
transaction-scoped advisory lock derived from normalized source/path. Concurrent identical
submits create one decision, intent, file result and index operation. Two proposals based on
the same old file serialize; one writes and the other observes a conflict. Refresh during an
apply waits for the row and cannot replace a successful result.

After apply, the source is securely reread and its hash must still equal the bound new hash.
`index_document` chunks exactly those bytes, computes embeddings, and deletes/reinserts only
that `(source_id, source_path)` in one PostgreSQL transaction. The unique source/path/ordinal
index prevents duplicates. No global production reindex is started.

## Browser and client boundary

Pending/deferred proposals show the complete immutable revision and full diff before
`Accept & Apply`, `Reject` and `Defer`. Old accepted records show „akzeptiert, noch nicht
angewendet“ and require `Apply accepted revision`. Pending/failed/crash states expose Apply
retry; applied/index-failed states expose index retry; conflicts expose both safe retry and
`Refresh review` until a successor is bound. Statuses, attempts, timestamps, bound revision,
truncated hashes and controlled error classes are rendered without host paths or private
exception text.

Every mutation is POST-only and covered by the existing session-bound CSRF middleware.
Ownership always comes from server configuration and is rechecked under database locks.
Hidden revision/status fields are compare-and-set observations, not authorization. GET remains
read-only, redirects stay internal, and duplicate submits are idempotent.

Telegram can only ask whether to create a proposal and can create a pending queue entry after
confirmation. Legacy decision/refresh/apply callbacks are informational only. MCP remains
read-only with its existing tools.

## Durable operations and known limits

Backup, restore, fingerprint, empty-target refusal, sequence repair and integrity checks now
include nonempty `proposal_applies`. Restore keeps apply/review/decision immutability triggers
and constraints enabled; a real injected mid-restore failure proves full rollback. The database
archive does not contain Markdown files, model cache or Git history: back up and verify the
separate host Knowledge tree independently and coordinate recovery by comparing journal hashes.

Hard process/host/power failure can occur between any two durable operations. Directory
`fsync` gives the strongest local-filesystem durability available, but storage hardware and
remote/network filesystems may provide weaker guarantees. The supported production model is a
local Linux filesystem with `renameat2`, stable inode semantics and trusted same-UID processes.
No global atomicity, automatic Git versioning or automatic production recovery is claimed.
