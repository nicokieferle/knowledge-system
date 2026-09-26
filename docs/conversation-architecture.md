# Conversation architecture

## V3.3 apply boundary

`review-web -> ProposalApplyService -> proposal_applies / SecureKnowledgeWriter /
index_document` is the only write path. Accept & Apply commits the immutable accepted
revision and the apply intent in one PostgreSQL transaction, then performs the separate
filesystem and index phases. The journal binds proposal, accepted review, logical target,
old/absent basis, new hash and actor. Successful apply/index states are terminal; attempts,
safe error classes and timestamps remain durable.

The three resources cannot form one global transaction. A retry before the rename sees the
old basis; a retry after a completed rename recognizes the exact expected new hash and
finishes idempotently. Any third state is a conflict and is preserved. Index failure leaves
the file applied and exposes a separate retry. Document indexing deletes and inserts only
the applied `(source_id, source_path)` inside one PostgreSQL transaction, so readers see the
complete old or new chunk set. Apply and index use one database advisory lock namespace per
normalized path as well as row locks.

An accepted conflict is never rebound or rewritten. „Refresh review“ atomically records one
owned successor proposal plus a control audit message, then uses the normal safe snapshot
path to prepare its first immutable revision. The original decision/apply conflict remains
unchanged. A double submit reuses that successor.

Telegram still reaches only proposal creation and cannot decide, apply, refresh a conflict
or retry indexing. MCP still exports only read operations.

## V3.2.1 client boundary (supersedes historical Telegram review below)

`TelegramAdapter -> ConversationService -> ProposalService` collects pending proposals.
`review-web -> ProposalReviewService -> PostgresReviewStore / GitMarkdownSource` is
the only review/decision client. Telegram never constructs a review service. Legacy
rv:/pv: callbacks are information-only before ID parsing or domain dispatch.

The web principal maps explicitly to the full existing client identity tuple. Queue
and provenance queries enforce that tuple, independent of active/recent topic limits.
Existing originating_message_ids load user messages inside the owned conversation;
no new durable table or migration is needed. See `v321-browser-review.md`.

## Boundary

V3.0 adds persistent conversation and proposal intent handling without coupling the core to
a messenger or LLM vendor:

```text
Telegram ----+
Web UI ------+--> ConversationService
Other client-+          |
                        +--> ConversationStore (durable raw messages)
                        +--> ConversationMemory (durable rolling summary)
                        +--> ChatModel protocol
                        +--> KnowledgeRetriever protocol --> KnowledgeService
                        `--> ProposalService (durable pending records)
```

Telegram is a thin V3.1 adapter. Generic `client_type`, external conversation IDs and
external message IDs preserve client references without putting Telegram concepts into core
logic. A different client can reopen the same server-side conversation if its adapter maps to
the same conversation ID.

Real LLM implementations are injected behind `ChatModel`, `ConversationSummarizer`,
`IntentClassifier` and `ProposalGenerator`. The core has no provider SDK, key or network call.
Unit tests use deterministic fakes.

## Three data classes

Raw conversation history is the complete persisted user, assistant and system message stream.
It is audit history and is never automatically canonical knowledge.

Conversation memory is a rolling summary plus full recent messages. It helps a model retain
longer context, but remains conversation state rather than canonical knowledge. Original
messages are never deleted when a summary is created.

Curated knowledge remains the reviewed Markdown under `knowledge/`. Retrieval results are
read through `KnowledgeService`; the ConversationService calls its injected narrow search
boundary directly, never via MCP or HTTP. A proposal is only a pending suggestion and cannot
write Markdown or Git.

## Memory invariants

The default policy keeps 12 recent messages and starts compaction after 30 unsummarized
messages. Tests can inject smaller values. When compaction begins, the store first returns an
immutable ordered message batch. Its last ID is the exact snapshot boundary passed to the
compare-and-set write. Messages arriving while the summarizer runs are therefore not marked
as summarized. A summary version prevents concurrent workers from overwriting a winner with
stale output.

`ConversationContext.recent_messages` always excludes `current_user_message`. The current
message consequently occurs exactly once in LLM context. Remaining messages after the summary
boundary are kept in full, so no unsummarized gap exists between summary and recent context.
If a concurrent worker has already advanced the shared summary through or beyond an older
request's current message, that request ignores the newer summary and reconstructs its context
from durable raw messages before its own immutable boundary.

## Proposal mechanisms

1. `/remember` and `/propose` bypass semantic intent classification and create one pending
   proposal from the relevant prior conversation context.
2. `CREATE_PROPOSAL` from the injected semantic classifier creates the same pending record.
3. `SUGGEST_PROPOSAL` creates only a persistent pending suggestion and a confirmation request.
   Explicit confirmation creates the proposal; rejection creates none.

Commands and confirmation messages are persisted for auditability, but remain control input.
The proposal records their `trigger_message_id` separately from `originating_message_ids`, and
`ProposalGenerator` receives only the relevant originating messages. A confirmed suggestion
has a stable unique `source_suggestion_id`. PostgreSQL locks the pending suggestion and writes
the proposal plus the `confirmed` transition in one transaction, making retries idempotent.

## Persistence and operations

The PostgreSQL instance now contains two categories with different operational guarantees:

- Rebuildable: `chunks`, retrieval indexes and `index_metadata`.
- Durable and backup-relevant: `conversations`, `messages`, `conversation_summaries`,
  `proposal_suggestions`, `proposals`, `client_states`, `client_conversations` and
  `client_message_bindings`, `proposal_reviews`, `proposal_decisions` and
  `proposal_applies`.

`knowledge index` is scoped to chunk tables. `knowledge init-db` uses additive `IF NOT EXISTS`
DDL and preserves durable rows. An index rebuild or reset must never drop, truncate or delete
durable tables. Deleting the PostgreSQL Docker volume is no longer a safe index-reset procedure
once conversations exist; operations need a database backup and restore plan.

## Future adapters

The Telegram adapter translates updates into `ConversationService` calls and renders the
structured `ConversationTurnResult`; it does not own history or intent state. The real LLM
provider implements the four existing protocols. Proposal review and approval are the
V3.2 stage. V3.3 adds the separately guarded file/index phases, but never performs Git
commits or pushes.

## V3.2 review boundary

`TelegramReview -> ProposalReviewService -> ReviewStore / ReviewSource` is independent
of conversation generation. A suggestion asks whether to generate a proposal; it is
not consent to modify knowledge. A proposal is the untrusted draft. A review revision
is the immutable concrete target/base/old/new/hash/diff that can be accepted.

Preparation reads only `GitMarkdownSource.snapshot`. The draft's base is ignored.
`pending -> accepted/rejected/deferred`, `deferred -> accepted/rejected` are supported;
accepted/rejected are terminal. Identical action+revision retries reuse the decision;
contradictory or superseded callbacks fail. Deferral does not reopen to pending.

The store locks the proposal and verifies the full client ownership relation under
the same transaction. Preparing revisions is compare-and-set against the prior
review ID. Decisions atomically append audit records and update status/accepted ID.
Revision and decision rows are immutable in the application and via DB triggers;
terminal proposal rows cannot be changed except for identical idempotency no-ops.

V3.3 must use the accepted revision's full new content, not the draft or new LLM
output. `check_basis` detects changed bytes or create-target appearance. It is a
read-time check, not a cross-filesystem/DB lock; V3.3 must recheck during atomic apply.
Accept itself has no Git, file-write, retrieval-index or MCP dependency.

Telegram sends a compact summary first. A diff fitting, with its header, into 3000
UTF-16 units is sent inline; a larger diff is one exact UTF-8 `.diff` document.
Revision buttons are sent only after successful complete transfer. At-least-once
delivery can duplicate a document on retry, but preparation remains idempotent and
does not add a review revision or decision.
# V3.1 client and routing layer

```text
Telegram long polling -> TelegramAdapter -> ConversationRouter -> ConversationService
                                                               -> ConversationMemory
                                                               -> KnowledgeService
                                                               -> provider-neutral LLM ports
                                                               -> ProposalService -> PostgreSQL
```

Telegram parses updates, maps IDs, handles commands/callbacks and formats replies. It has
no SQL, memory, retrieval, proposal-generation, or prompt logic. Generic `client_states`
stores the active conversation per `(client_type, external_chat_id, external_user_id)`;
`client_conversations` records ownership and `client_message_bindings` makes Telegram
message retries stay attached to their original topic. PostgreSQL advisory locking
serializes active-topic changes.

The router supplies the model with at most 20 recently active owned conversations, never
their complete histories. A switch target is accepted only if it is among those owned,
non-archived candidates. Invalid model IDs fail closed by creating an isolated topic.
Manual commands bypass routing entirely.

Each conversation's memory builder retains the V3.0 snapshot boundary and exactly-once
current-user-message invariant, so investment content cannot enter a Knowledge-System
topic merely because both came from one Telegram chat. Memory is not canonical knowledge.
Suggestions are durable pending actions; inline callbacks carry only action + UUID and the
proposal store performs ownership, status, and idempotency checks. A pending proposal is
still not a knowledge or Git write.
