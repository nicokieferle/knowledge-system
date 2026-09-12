# V3.2 durable backup and restore hardening

## Reproduced PostgreSQL 17 behavior

The durable backup is a custom-format, data-only archive of exactly these ten tables, in this
TOC order:

1. `conversations`
2. `client_states`
3. `client_conversations`
4. `client_message_bindings`
5. `messages`
6. `conversation_summaries`
7. `proposal_suggestions`
8. `proposals`
9. `proposal_reviews`
10. `proposal_decisions`

`messages_id_seq` is also present. PostgreSQL 17.11 reports the circular-foreign-key warning
for `proposals` and `proposal_reviews`. A restore into an initialized V3.2 schema without
special handling fails while loading an accepted proposal because its referenced review row
comes later in the archive. `--single-transaction` correctly leaves zero restored rows after
that failure. `pg_restore --list` succeeds in both cases, so it proves archive readability but
not recoverability.

## Resolution

The relationship itself is intentionally cyclic. The migration therefore marks only
`proposals_accepted_review_fk` as `DEFERRABLE INITIALLY DEFERRED`. The proposal row can be
loaded before its accepted immutable review, while PostgreSQL still validates the relationship
at transaction commit. All other foreign keys remain immediate.

The restore no longer uses `--disable-triggers`. It keeps the review/decision immutability
triggers and the terminal-proposal guard enabled throughout. It generates SQL only for the
fixed durable table allowlist into a private temporary file inside the PostgreSQL container.
One `psql --single-transaction` invocation then performs:

1. all allowlisted table data;
2. the explicit, transactional `messages_id_seq` correction;
3. the durable relationship integrity guard; and
4. commit-time validation of the deferred accepted-review foreign key.

The archive TOC must contain exactly one data entry for each of the ten tables. Archive
`SEQUENCE SET` entries are excluded because PostgreSQL `setval()` changes are not rolled back;
the script instead uses transactional `ALTER SEQUENCE ... RESTART` based on the restored
maximum message ID.

`ON_ERROR_STOP` makes any SQL error abort the operation. A load error, integrity exception or
deferred-constraint failure rolls back data and sequence changes together. No archive content
is evaluated by a host shell, the archive path is quoted and checked for readability, and the
temporary SQL file is mode-restricted and removed by a trap. Before `psql --no-psqlrc` reads
the generated SQL, the script requires PostgreSQL 17's generated `\\restrict` and
`\\unrestrict` guards exactly once. Archive data therefore cannot inject unrestricted psql
meta-commands such as host shell execution.

The generic `pg_dump` warning remains visible; the solution does not suppress it. A successful
archive listing remains a useful quick check, but a backup is considered recoverable only after
an isolated initialized-schema restore and an exact source/restore durable fingerprint match.

PostgreSQL semantics used here are documented in the official
[`pg_dump`](https://www.postgresql.org/docs/17/app-pgdump.html),
[`pg_restore`](https://www.postgresql.org/docs/17/app-pgrestore.html),
[`psql`](https://www.postgresql.org/docs/17/app-psql.html), and
[`SET CONSTRAINTS`](https://www.postgresql.org/docs/17/sql-set-constraints.html) documentation.

## Local validation

- PostgreSQL 17.11, isolated loopback-only tmpfs container.
- Exact warning reproduced before the change.
- Baseline restore without trigger disabling failed at `proposals_accepted_review_fk`.
- Representative roundtrip: all ten tables non-empty, two immutable revisions for the accepted
  proposal, an additional deferred proposal, two decisions, identical counts and SHA-256
  fingerprint after restore.
- All foreign keys validated; only the accepted-review FK deferred; all three immutability
  triggers enabled and enforced after restore.
- A synthetic final failure after data loading and transactional sequence restart left all ten
  tables empty and restored the initial sequence state.
- Full suite: 322 passed, 3 platform-specific Windows skips.
