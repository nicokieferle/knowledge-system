# V3.2 implementation decision

**Historical V3.2 client design:** V3.2.1 supersedes the Telegram UI, callback and
implicit-first-display preparation described below. Reviews/decisions now exist
only in the private browser client; all preparation requires POST. Domain values,
immutability, path validation and decision transactions remain authoritative.
See [V3.2.1](v321-browser-review.md).

Base: `92b25474b94a3d4a369e50363cc3df68a2fba62c` (expected main).

The existing proposal is an untrusted draft, not an approved patch. Keep it intact.
Introduce append-only review revisions and an append-only decision journal. Serialize
preparation and decisions on the proposal row, and authorize using the complete
client_conversations ownership key in the backend transaction, not the topic limit.

Use SHA-256 of exact UTF-8 bytes, with an explicit absent base for creates. Store
old/new text and deterministic diff. Never trust the draft's base_revision. Accept
requires a specific current review ID and a fresh source check. V3.3 must repeat
that check atomically with a guarded apply; V3.2 never writes files or indexes.

No extra proposal statuses: pending/deferred can be reviewed, accepted/rejected
are terminal. Preparing a revision is explicit or idempotently performed on first
display. Refresh requires an explicit new preparation; old callbacks cannot approve
the replacement. Telegram shows a small complete diff inline and sends a larger
complete diff as one in-memory UTF-8 document. Buttons follow only after successful
delivery of the full inline diff or document. No truncated-diff Accept button.

Migration replaces only the known proposals_status_check constraint atomically,
preserves rows, and adds revision/decision tables to all durable operations. Legacy
drafts without valid targets fail safely and can be prepared with explicit corrected
input through the client-neutral service; no LLM runs during review or accept.

## Contracts and limits

- `get`, `list`, `prepare`, `decide` and `decide_revision` return structured domain
  values. `check_basis` is the narrow V3.3 read-only stale check.
- Identity must come from a trusted authenticated client, never a user-supplied
  claim. SQL checks all three client identity fields against conversation ownership.
- Source paths are relative to the configured knowledge root, not OS paths. Review
  accepts a conservative portable ASCII subset: no hidden components, backslashes,
  colons, trailing dots/spaces, reserved Windows device names, traversal, root README
  or non-.md files. This is intentionally narrower than discovery, not broader.
- Linux reads pin directory descriptors with O_NOFOLLOW; Windows rejects symlinks
  and junctions before/after reading. The configured root is trusted. V3.3 still
  needs atomic apply and protection against concurrent trusted checkout changes.
- Each old/new content is limited to 128,000 UTF-8 bytes. Unsupported controls and
  invisible directional characters fail closed. No-op/empty create is rejected.
- The stored human-readable unified diff escapes backslash, CR and tab reversibly,
  and marks missing final newlines. It is a display format, NOT an apply patch.
  V3.3 must use the exact stored full new content/hash, never interpret this display
  diff as raw file bytes. No ANSI, normalization or silent newline conversion.
- Telegram uses a 3000 UTF-16-unit inline message threshold including the diff
  header. Larger diffs use `sendDocument` with exact `diff.encode("utf-8")` bytes.
  Summary, diff/document and buttons require at most three API calls per review.
  Buttons follow only after complete delivery. Delivery remains at-least-once;
  a document retry can be visibly duplicated without changing durable state.
- Callback data contains action plus immutable review UUID (37 ASCII bytes). Legacy
  unprepared proposals support only Reject/Defer via a proposal UUID, never Accept.
- A new revision does not mutate its predecessor or reset deferred to pending.
  Old revision callbacks conflict; repeat decisions on the original revision are
  idempotent. Expected-status CAS prevents a concurrent defer/accept overwrite.
- Raw ProposalGenerator output is strictly typed but still untrusted. It supplies
  full candidate Markdown, not an authoritative patch/base. Invalid legacy targets
  need an explicit correction through the trusted service API. No LLM auto-repair.

## Deployment boundary

This development branch is not a production rollout. Migration expands durable
data from eight to ten tables. Run old pre-migration backup before switching tools;
run new init-db and new backup only under a separately reviewed deployment gate.
V3.3 must address atomic Git apply, conflict handling and reindex-after-merge.
