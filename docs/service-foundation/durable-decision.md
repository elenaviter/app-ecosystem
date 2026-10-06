# Durable decision coordination

`service_foundation.coordination` provides a generic coordinator and a
PostgreSQL decision store for workflows that prepare effects in one or more
trusted participants. The application supplies participant adapters, verifies
their receipts, authorizes a commit witness, and schedules recovery. No
participant effect is released on a timeout or missing remote reply alone.

The PostgreSQL store persists canonical intent bytes, immutable prepare and
finish receipts, one terminal decision by conditional SQL update, database-time
expiry, and bounded enumeration of unfinished transactions. A committed
decision requires every named participant's prepared receipt and a nonempty
lowercase SHA-256 witness digest. Recovery presumes abort only after durable
expiry, then asks every participant to finish, including one whose stage
receipt was lost before central recording. Participant finish must be
idempotent and acknowledge an unknown aborted transaction as a tombstone.

## Authority wire bytes

The v2 integration target uses the exact seven-key global intent schema
`durable-transaction-intent.v1`: `schema`, `transaction_id`, `epoch`,
`request_id`, `expires_at`, ordered `participants`, and opaque full `payload`.
`transaction_id` and `epoch` are bound before hashing; `expires_at` is an
integral UTC Unix second. The payload contains selected participant inputs.
Each participant projection adds the global intent digest to its exact selected
input from the persisted intent. A later caller cannot replace that input by
presenting a self-consistent projection and digest.

`durable_wire.canonical_json_bytes` emits UTF-8 with Unicode code-point-sorted
object keys (the Python `sort_keys` order, not RFC 8785 UTF-16 order),
significant array order, no whitespace, no Unicode normalization, and only
JSON null, booleans, strings, arrays, objects, and integers. It rejects floats,
NaN, Infinity, non-string keys, and lone surrogates. The parser also rejects
duplicate keys and alternate encodings. The frozen cross-application test
vector in `packages/service-foundation/tests/test_durable_wire.py` has global
intent digest `1192d0b514933591e4b1c35e5de427697f4e781ab4d525e52138af8d8b5b6d51`
and Card projection digest
`436fef32e30b23c40ea6eae2329a6d4843ed00b59f409290cb50e2b46d62c663`.
`verify_participant_projection` compares a supplied projection to the selected
persisted input. Before revision zero is accepted only for action `create`;
candidate revisions and every dependency revision must be positive integers,
excluding booleans.

The v2 source increment is in `service_foundation.coordination.durable_decision_v2`.
Its `IntentDraft` captures the full payload before awaiting storage, and
`PostgresDecisionStore.begin(draft, transaction_id=None, epoch=None,
connection=None)` binds both coordinates before computing the global digest.
Coordinates must be supplied together or both omitted. Standalone calls
allocate an ID and epoch; a caller may reserve them in its own outer SQL
transaction. When passed a connection already in a transaction, the store
uses a nested savepoint and never commits or closes the caller's connection.
Store-allocated epochs come from a PostgreSQL sequence: they increase but
may have gaps after a rollback. Callers requiring transactional reservation
must supply their own durable ID and epoch together.
Receipt writes and terminal decisions offer the same optional connection.
The coordinator's `prepare_existing(transaction_id)` starts remote stages
only after the caller's reservation transaction has committed.

The generic table is `service_foundation_decisions`, keyed by
`(namespace, transaction_id)` and unique on
`(namespace, replay_scope, request_id)`. `replay_scope` is an opaque
application-computed identity boundary outside the seven-key wire intent.
The table holds exact canonical intent bytes, the bound epoch, database-time
expiry and decision timestamp, prepared and finished receipts and their
counts, and the witness digest. It also has a unique
`(namespace, replay_scope, epoch)` key. Receipt writes merge one immutable
JSONB entry with a conditional SQL update, preserving concurrent receipts.
The store selects raw JSONB text and parses it once, independently of an
asyncpg pool's JSONB decoder. This also refuses a legacy whole-column JSON
string instead of parsing it twice as a receipt object. It accepts a decoded
mapping when a row is supplied directly, and casts serialized receipt text
to JSONB in SQL so platform pool encoders cannot turn receipt objects into
JSON strings.
An expired but still undecided COMMIT reports `commit_expired`; subsequent
recovery can durably presume abort. A recovery pass continues past a failed
participant, then raises `RecoveryIncomplete` with the failed transaction IDs
and completed records so the caller can retry. Application tables may
reference the one generic decision row; the generic table contains no
application policy.

`durable_decision_log` re-exports this one v2 implementation as the stable
import path. The pre-v2 draft has been removed, with its protocol regressions
migrated to v2 tests. The source is not yet an installed application
integration contract or verified live runtime.

## Authenticated participant answers

`service_foundation.coordination.participant_answer` owns a product-neutral
response envelope and HMAC frame. It neither authenticates incoming requests
nor selects peers, secret references, operation names, authority scope or
result/refusal policy. Those remain in application adapters. The seven-field
durable `Receipt`, decision ledger and transaction transitions do not change.

`request_digest(fields)` hashes exactly these eight unsigned request fields:
`schema`, `action`, `request_echo`, `scope`, `transaction_id`, `decision`,
`limit`, `cursor`. Keep inactive fields explicitly null. Select those fields
before calling: a transport request containing `service_proof` is refused,
not silently stripped. `request_echo` is 32–128 lowercase hexadecimal
characters; the caller generates a fresh unpredictable value on every attempt,
including a retry. The helper checks its shape, not its source of randomness.
Applications validate action-specific field semantics before sending/serving.

The unsigned answer contains exactly `schema`, `direction`, `audience`,
`request_echo`, `request_digest`, `action`, `scope`, `transaction_id`,
`decision`, `limit`, `cursor`, and an opaque object `result`.
`sign_participant_answer(unsigned, schema=..., secret=..., signer_id=...,
timestamp=...)` returns a three-field proof: `service_id`, `timestamp`,
`signature`. Add it as `receipt_proof` to the unsigned answer. The HMAC-SHA256
input is the following five UTF-8 lines, joined by four newline bytes without a
trailing newline:

```text
trusted response schema
trusted signer ID
canonical decimal UTC Unix seconds
request_echo
lowercase SHA-256 of canonical unsigned-answer bytes
```

The signature is unpadded base64url. Keys are explicit UTF-8 strings or bytes,
at least 32 bytes long; there is no generated key or unsigned fallback.
Trusted framing strings are bounded printable nonempty strings, excluding
newlines. A timestamp has no leading zero except the value `0` itself.

`verify_participant_answer(answer, schema=..., secret=..., signer_id=...,
audience=..., direction=..., request=..., now=..., max_skew=300)` takes the
**inner signed envelope**, the frozen eight-field request from this attempt,
and trusted local peer/key/audience/direction configuration. It checks exact
field sets, expected schema and signer, every request echo and digest, JSON
type-sensitive equality (boolean `true` is not integer `1`), inclusive clock
skew of at most 300 seconds and a constant-time signature comparison. It
returns a detached authenticated result object. Missing/invalid configuration,
unsigned transport responses and malformed, mismatched, stale or tampered
envelopes raise finite `ParticipantAnswerRefused`; these are local verification
failures, **not authenticated semantic participant refusals**.

The application must still validate its result tagged union, finite refusal
codes, bounded status, and each seven-field Receipt. Authentication does not
turn a signed refusal into success. An unsigned outer transport `ok` flag is
never authority; derive the outcome from the verified result. An unsigned
refusal or unknown transport outcome remains unavailable/pending rather than
evidence for a definitive rejection or a new decision. Request admission,
nonce replay storage, durable scope partitioning, decision comparison and
restart recovery remain application responsibilities.

Shared synthetic producer/consumer vectors are in
`packages/service-foundation/tests/fixtures/participant_answer_vectors.json`:
request digests, receipt, pending-null, page and refused answers, every signed
field tamper, new-echo replay, and wrong audience/direction. The fixture uses a
public test-only key, never a production credential. Consumer adapters load
these same vectors and validate their own result semantics on top.

## Bounded recovery pages

The legacy `list_in_doubt(limit=...)` and `Coordinator.recover(limit=...)`
still refuse `recovery_unbounded` before processing anything when the namespace
backlog exceeds the limit. They have not silently become truncated reads.

For a larger backlog, `PostgresDecisionStore.list_in_doubt_page(limit=...,
after="")` returns `(records, has_more)`. Its namespace-bound query uses an
exclusive `transaction_id > after` cursor, the same database ordering on
`transaction_id`, and `limit + 1` rows to determine `has_more`. It decodes at
most `limit` records. Limits are integers from 1 through 1000 (not booleans);
`after` must be a string of at most 128 characters.

`Coordinator.recover_page(limit=100, after="")` returns
`(records, next_after, has_more)`. It uses exactly the ordinary recovery
transition and receipt checks: finish terminal decisions, durably abort expired
undecided transactions, and keep unexpired transactions untouched. The optional
`PagingDecisionStore` capability extends, but does not change, `DecisionStore`.
A legacy-only store refuses `recovery_paging_unavailable` for the new operation.

`next_after` is the last **scanned** transaction ID, including a failed or
unexpired row. An empty page returns `([], "", False)`. Failures still raise
`RecoveryIncomplete` after continuing through the page; its additive
`next_after` and `has_more` fields carry the continuation even when the last
row failed. Those keyword-only fields default to `None` for legacy recovery.

The application owns scheduling, a maximum pages-per-pass budget, and cursor
storage. Advance from either a successful result or a page failure's metadata;
wrap to `""` after `has_more` is false. Wrapping retries failed/unexpired rows
and finds IDs inserted behind the cursor. Pages are not one global snapshot.
Completion survives process restart through the existing durable finish
receipts, even when a caller loses its cursor and restarts at `""`. A cursor is
neither a completion acknowledgement nor another decision ledger. Participant
finish must remain idempotent if the process stops after applying an effect but
before recording its receipt.
