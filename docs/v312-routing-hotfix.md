# V3.1.2 routing normalization

## Root cause and incident evidence

The routing model can return CONTINUE_CURRENT together with the active
conversation UUID. The domain decision intentionally forbids conversation_id for
CONTINUE_CURRENT. The provider now tolerates only the benign redundant-active-ID
case by normalizing it to null while retaining strict validation for contradictory IDs.

User-supplied production diagnostics reported `LLMResponseError`,
`phase=process_message`, `http_status=unknown`, with no message binding yet.
An isolated routing probe passed without candidates but failed with four topics.
The safe schema diagnosis showed a valid continue action, an offered UUID,
null title and finite in-range numeric confidence. This hotfix tests the confirmed
active-UUID response shape with synthetic data; no production data or server was accessed.

## Narrow provider boundary fix

After JSON type checks and UUID parsing, normalization requires exactly one active
candidate, an exact UUID match, and exactly one occurrence of that UUID in the
candidate list. Missing active candidates, foreign/inactive IDs, multiple active
topics and duplicate/conflicting entries remain sanitized `LLMResponseError`s.
The domain invariant is unchanged: only SWITCH_TO_EXISTING can carry an ID.
START_NEW_CONVERSATION with an ID still fails. Type, UUID and confidence validation
remains strict. The prompt now states the per-action ID requirements explicitly,
alongside title and numeric confidence guidance; parsing does not trust compliance.

## Candidate safety decision

SWITCH_TO_EXISTING keeps its existing behavior. The provider parses the UUID;
the router resolves it only among the current client's offered conversations.
An unoffered UUID fails closed into a new isolated topic, never the referenced
foreign topic. Changing that fallback to a provider error would change established
behavior and is unnecessary for this incident. A regression test uses an existing
other-client topic to verify this boundary. The new CONTINUE_CURRENT normalization
does not use that fallback and does not create a topic.

## Regression and scope

The local incident regression uses the real provider parser, router, Telegram
adapter, conversation service and polling loop with fake transport/model responses
and in-memory stores. Four topics, exactly one active, and an initially absent
binding reproduce the incident. After normalization, the active topic receives
the binding and one user/assistant turn; the polling offset advances without retry.
A repeated update after an active-topic change retains its binding/result without
another model call or a new topic. The test failed with the original provider.

No DB production logic, schema, MCP, retrieval, write/approval behavior or
infrastructure changed. PostgreSQL integration need not be restarted for this
provider-only fix; skipped DB tests are not claimed as freshly passed.

## Local validation

- Targeted provider/router/polling tests: 100 passed.
- Full pytest: 236 passed, 13 PostgreSQL tests skipped (no DB opt-in).
- Ruff check, Ruff format check, Git diff check and compileall: PASS.
- The initial full-suite attempt hit Windows permissions in the existing pytest
  temporary directory. A fresh isolated temp/cache path passed without code changes.
- Self-review checked exact active-ID matching, duplicate/ambiguous candidate
  rejection, unchanged domain invariants, cross-client switch isolation and absence
  of topic creation in the normalized continuation path. No blocking finding.
