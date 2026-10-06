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
