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

`durable_wire.canonical_json_bytes` emits UTF-8 with sorted object keys,
significant array order, no whitespace, no Unicode normalization, and only
JSON null, booleans, strings, arrays, objects, and integers. It rejects floats,
NaN, Infinity, non-string keys, and lone surrogates. The parser also rejects
duplicate keys and alternate encodings. The frozen cross-application test
vector in `packages/service-foundation/tests/test_durable_wire.py` has global
intent digest `1192d0b514933591e4b1c35e5de427697f4e781ab4d525e52138af8d8b5b6d51`
and Card projection digest
`436fef32e30b23c40ea6eae2329a6d4843ed00b59f409290cb50e2b46d62c663`.

The initial coordinator and store source checkpoint predates these wire rules.
Until its DTO and SQL record are migrated, the source is a reusable protocol
draft and is not an application integration contract or installed runtime.
