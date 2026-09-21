# V3.1.1 Telegram retry diagnostics

Validation update: the subsequent gate on `10512691a61dcfd0c411c05afa0be624a34b8b4e`
passed all 13 PostgreSQL 17.11 integration tests and all 196 full-suite tests,
with zero failures/skips. Earlier counts below are historical intermediate results.

## Observed incident and limits

The reported V3.1 production log repeats `Telegram polling cycle failed; retrying`
for approximately eight minutes, then reports a pending suggestion. The exception
class, failing operation and HTTP status were discarded. The original cause cannot
be determined from these lines: neither a rate limit nor a provider parsing,
network, retrieval or database failure is established. The suggestion log proves
only that its database transaction completed, not that Telegram delivery succeeded.

## Message flow and durable boundaries

`getUpdates` returns a serial batch. Normal messages enter the adapter, invoke the
routing model, create/select a conversation and persist active topic/ownership.
The adapter then persists the message-to-conversation binding. These are separate
transactions. ConversationService inserts the user message with an external ID,
loads history (possibly persisting an LLM-generated summary through CAS), and
classifies intent. Chat retrieves knowledge, generates an answer and persists the
assistant message. Explicit proposals retrieve historical context, generate a
draft and persist a proposal. Suggestions persist a suggestion and then an
assistant confirmation in separate transactions; they do not run retrieval or
proposal generation yet. `/remember` and `/propose` bypass intent classification.
Only after `sendMessage` returns does the polling loop advance its in-memory offset.

Callbacks insert a deduplicated user control message. Save retrieves context and
generates a draft; the store locks the suggestion and atomically creates its
proposal and resolves its state. Reject resolves the suggestion under a lock.
`answerCallbackQuery` precedes offset advancement. No transaction spans a whole
update or combines PostgreSQL with Telegram delivery.

## Fixes

- Log exception class, phase (`poll`, `process_message`, `process_callback`), numeric
  HTTP status when available, and retry delay. Do not render exception messages,
  chained exceptions or tracebacks. Even provider exception constructors accept
  arbitrary strings, so this avoids relying on a sanitization convention.
- Retry at 3, 6, 12, 24, 30 seconds, capped at 30. Reset after a handled update or
  successful empty poll. A successful fetch followed by failed handling does not
  reset the delay. Failed updates never advance the offset.
- Look up a durable binding before routing again. This also protects `/new`
  retries after the binding committed.
- Give suggestion assistant messages the existing `response:<external-id>` key.
- Replay stored proposals, suggestions and assistant responses before model calls.
  Recover a suggestion committed before its assistant insert. This prevents a new
  classifier result from changing an already persisted outcome on a serial retry.

  Processing becomes deterministic once the relevant durable binding/result exists.
  Classification before result persistence may be repeated after a failure.
  The binding fixes the topic, not an unfinished intent-classification result.
- Replay the proposal for the same confirmation message before retrieval/generation
  when a callback acknowledgement failed after commit.

## Idempotency assessment

**Confirmed bug:** repeated suggestions previously appended duplicate assistant
confirmations without an external ID. Retrying routing could also create unused
topics and change active state before the old binding was returned. Both are fixed
for retries with a committed binding. User messages, suggestions, explicit
proposals and confirmed proposals retain their existing database unique keys.
Chat assistant messages already had a unique response key but previously repeated
the provider work. Completed result replay now avoids that work and intent drift.

Remaining boundaries:

- Telegram delivery remains at-least-once. If Telegram accepts a send but its reply
  is lost, retry can deliver the same text twice; there is no cross-system atomic
  acknowledgement. This patch does not claim exactly-once delivery.
- Topic creation/activation and binding are still separate transactions. A crash
  before binding commits can leave an unused topic. Parallel workers can also
  race before binding; the existing serial single-poller assumption remains.
- Existing historical duplicate rows are not rewritten. Old suggestion assistant
  messages without a response ID can acquire one additional keyed confirmation
  if an old update is replayed across the upgrade.
- Permanent errors still block the serial queue, now with useful diagnostics and
  slower capped retries. Skipping updates without a durable failure queue would
  risk loss and is deliberately outside this hotfix.
- Repeated distinct save callbacks still reach existing store-level idempotency;
  unlike the identical callback, they may repeat retrieval/provider work. Expired
  callback acknowledgements can remain a permanent Telegram error.
- Phases locate the adapter operation, not every internal retrieval/model stage.

No database migration, retrieval change, MCP contract change or deployment is part
of this patch. Tests inject provider/DB/transport errors and inspect captured logs
for sensitive strings, retry delays, offsets and durable side-effect counts.

Validation: the full local suite passes (170 passed, 1 skipped). The skipped test
requires an explicitly configured isolated PostgreSQL database (`TEST_DATABASE_URL`).
The initial run encountered Windows permissions on the existing pytest temporary
directory; rerunning with a fresh temporary directory and cache disabled succeeded.
Real PostgreSQL and Telegram E2E validation remain for the controlled test stage.
