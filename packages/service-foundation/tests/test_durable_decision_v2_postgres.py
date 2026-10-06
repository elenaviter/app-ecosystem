"""The v2 generic store against disposable PostgreSQL, when a DSN is set."""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest
import pytest_asyncio

from service_foundation.coordination.durable_decision_v2 import (
    Coordinator, DecisionRefused, PostgresDecisionStore, Receipt,
)
from service_foundation.coordination.durable_wire import (
    IntentDraft, participant_projection, projection_digest,
)


@pytest_asyncio.fixture
async def store():
    dsn = os.environ.get("SERVICE_FOUNDATION_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("requires a disposable PostgreSQL DSN")
    import asyncpg

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=6)
    schema = "w581v2_" + uuid.uuid4().hex
    async with pool.acquire() as connection:
        await connection.execute(f"CREATE SCHEMA {schema}")
    result = PostgresDecisionStore(pool, schema=schema, namespace="test")
    await result.ensure_schema()
    await result.ensure_schema()
    try:
        yield result
    finally:
        async with pool.acquire() as connection:
            await connection.execute(f"DROP SCHEMA {schema} CASCADE")
        await pool.close()


@pytest_asyncio.fixture
async def platform_codec_store():
    dsn = os.environ.get("SERVICE_FOUNDATION_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("requires a disposable PostgreSQL DSN")
    import asyncpg

    async def install_codecs(connection):
        for type_name in ("json", "jsonb"):
            await connection.set_type_codec(
                type_name, schema="pg_catalog", encoder=json.dumps,
                decoder=json.loads)

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3,
                                     init=install_codecs)
    schema = "w581codec_" + uuid.uuid4().hex
    async with pool.acquire() as connection:
        await connection.execute(f"CREATE SCHEMA {schema}")
    result = PostgresDecisionStore(pool, schema=schema, namespace="codec_test")
    await result.ensure_schema()
    try:
        yield result
    finally:
        async with pool.acquire() as connection:
            await connection.execute(f"DROP SCHEMA {schema} CASCADE")
        await pool.close()


def draft(*, request_id="req", expires_at=2000000000, names=("card",)):
    inputs = {}
    for name in names:
        inputs[name] = {
            "participant": name, "binding_kind": "card", "binding_ref": name,
            "target_scope": "project-a", "target_incarnation": "inc-a",
            "action": "update", "before_revision": 2, "candidate_revision": 3,
            "candidate_digest": "a" * 64, "dependency_revisions": {},
            "actor_subject": "user-a", "actor_kind": "human", "provisioning": None,
        }
    return IntentDraft("project-a:user-a", request_id, expires_at, names,
                       {"participant_inputs": inputs, "project_ref": "project-a"})


class Realm:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self.finishes = 0

    async def _receipt(self, transaction_id, digest):
        record = await self.store.read(transaction_id)
        intent = record.intent
        return Receipt(transaction_id, intent.epoch, intent.digest, self.name,
                       projection_digest(intent, self.name),
                       participant_projection(intent, self.name)["candidate_digest"],
                       digest)

    async def prepare(self, transaction_id):
        return await self._receipt(transaction_id, "b" * 64)

    async def finish(self, transaction_id, decision):
        self.finishes += 1
        return await self._receipt(transaction_id, "c" * 64)


class Verifier:
    async def prepared(self, record, receipt):
        if receipt.receipt_digest != "b" * 64:
            raise DecisionRefused("untrusted_receipt")

    async def finished(self, record, receipt):
        if receipt.receipt_digest != "c" * 64:
            raise DecisionRefused("untrusted_receipt")


def receipt_for(record, name, digest="b" * 64):
    return Receipt(record.transaction_id, record.intent.epoch,
                   record.intent.digest, name,
                   projection_digest(record.intent, name),
                   participant_projection(record.intent, name)["candidate_digest"],
                   digest)


@pytest.mark.asyncio
@pytest.mark.parametrize("prepared_count", [0, 1])
@pytest.mark.parametrize("caller_connection", [False, True])
async def test_store_commit_refuses_missing_preparations(
        store, prepared_count, caller_connection):
    """The store guard matters even when an app calls decide directly."""
    names = ("card", "business")
    request_id = f"guard-{prepared_count}-{caller_connection}"

    async def exercise(connection=None):
        row = await store.begin(draft(request_id=request_id, names=names),
                                transaction_id=f"tx-{request_id}",
                                epoch=20 + prepared_count * 2 + caller_connection,
                                connection=connection)
        if prepared_count:
            await store.record_prepared(receipt_for(row, names[0]),
                                        connection=connection)
        before = await store.read(row.transaction_id, connection=connection)
        with pytest.raises(DecisionRefused, match="commit_unprepared"):
            await store.decide(row.transaction_id, "committed",
                               witness_digest="e" * 64, connection=connection)
        assert await store.read(row.transaction_id, connection=connection) == before
        for name in names[prepared_count:]:
            await store.record_prepared(receipt_for(row, name),
                                        connection=connection)
        committed = await store.decide(row.transaction_id, "committed",
                                       witness_digest="e" * 64,
                                       connection=connection)
        assert committed.state == "committed"
        assert set(committed.prepared) == set(names)
        return row.transaction_id

    if caller_connection:
        async with store.pool.acquire() as connection:
            async with connection.transaction():
                transaction_id = await exercise(connection)
    else:
        transaction_id = await exercise()
    assert (await store.read(transaction_id)).state == "committed"


@pytest.mark.asyncio
async def test_platform_jsonb_codec_standalone_and_caller_transaction(platform_codec_store):
    store = platform_codec_store
    first = await store.begin(draft(request_id="codec-standalone"))
    prepared_receipt = await Realm(store, "card").prepare(first.transaction_id)
    assert (await store.record_prepared(prepared_receipt)).state == "prepared"
    assert (await store.decide(first.transaction_id, "committed",
                               witness_digest="e" * 64)).state == "committed"
    finished_receipt = await Realm(store, "card").finish(first.transaction_id,
                                                          "committed")
    assert (await store.record_finished(finished_receipt)).finished["card"] == finished_receipt

    async with store.pool.acquire() as connection:
        value_types = await connection.fetchrow(
            f"""SELECT jsonb_typeof(prepared->'card') AS prepared_type,
                jsonb_typeof(finished->'card') AS finished_type
                FROM {store.table} WHERE namespace=$1 AND transaction_id=$2""",
            store.namespace, first.transaction_id)
    assert value_types["prepared_type"] == "object"
    assert value_types["finished_type"] == "object"

    async with store.pool.acquire() as connection:
        with pytest.raises(RuntimeError, match="rollback"):
            async with connection.transaction():
                second = await store.begin(
                    draft(request_id="codec-caller"), transaction_id="codec-caller-tx",
                    epoch=123, connection=connection)
                prepared_receipt = Receipt(
                    second.transaction_id, second.intent.epoch, second.intent.digest,
                    "card", projection_digest(second.intent, "card"),
                    participant_projection(second.intent, "card")["candidate_digest"],
                    "b" * 64)
                assert (await store.record_prepared(
                    prepared_receipt, connection=connection)).state == "prepared"
                assert (await store.decide(
                    second.transaction_id, "aborted",
                    connection=connection)).state == "aborted"
                finished_receipt = Receipt(
                    second.transaction_id, second.intent.epoch, second.intent.digest,
                    "card", projection_digest(second.intent, "card"),
                    participant_projection(second.intent, "card")["candidate_digest"],
                    "c" * 64)
                assert (await store.record_finished(
                    finished_receipt, connection=connection)).finished["card"] == finished_receipt
                assert (await store.read(second.transaction_id,
                                         connection=connection)).terminal
                raise RuntimeError("rollback")
    assert await store.read("codec-caller-tx") is None


@pytest.mark.asyncio
async def test_receipts_round_trip_between_default_and_platform_codecs(
        platform_codec_store):
    import asyncpg

    codec_store = platform_codec_store
    plain_pool = await asyncpg.create_pool(
        os.environ["SERVICE_FOUNDATION_TEST_POSTGRES_DSN"], min_size=1, max_size=2)
    plain_store = PostgresDecisionStore(
        plain_pool, schema=codec_store.schema, namespace=codec_store.namespace)
    try:
        for writer, reader, request_id in (
                (plain_store, codec_store, "plain-writer"),
                (codec_store, plain_store, "codec-writer")):
            row = await writer.begin(draft(request_id=request_id))
            receipt = receipt_for(row, "card")
            prepared = await writer.record_prepared(receipt)
            assert prepared.prepared["card"] == receipt
            assert (await reader.read(row.transaction_id)).prepared["card"] == receipt
            assert any(candidate.transaction_id == row.transaction_id
                       for candidate in await reader.list_in_doubt(limit=10))
            assert (await reader.decide(row.transaction_id, "committed",
                                        witness_digest="e" * 64)).terminal
            finished = receipt_for(row, "card", "c" * 64)
            await reader.record_finished(finished)
            assert (await writer.read(row.transaction_id)).finished["card"] == finished
            async with plain_pool.acquire() as connection:
                types = await connection.fetchrow(
                    f"""SELECT jsonb_typeof(prepared) AS prepared_column,
                        jsonb_typeof(prepared->'card') AS prepared_value,
                        jsonb_typeof(finished) AS finished_column,
                        jsonb_typeof(finished->'card') AS finished_value
                        FROM {writer.table} WHERE namespace=$1 AND transaction_id=$2""",
                    writer.namespace, row.transaction_id)
            assert set(types.values()) == {"object"}
    finally:
        await plain_pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_shape", [
    "value_string", "column_string", "wrong_key", "extra_field", "missing_field",
])
async def test_malformed_stored_receipt_fails_closed_across_codecs(
        platform_codec_store, bad_shape):
    import asyncpg
    from dataclasses import asdict

    codec_store = platform_codec_store
    plain_pool = await asyncpg.create_pool(
        os.environ["SERVICE_FOUNDATION_TEST_POSTGRES_DSN"], min_size=1, max_size=2)
    plain_store = PostgresDecisionStore(
        plain_pool, schema=codec_store.schema, namespace=codec_store.namespace)
    try:
        row = await plain_store.begin(draft(request_id=f"bad-{bad_shape}"))
        fields = asdict(receipt_for(row, "card"))
        if bad_shape == "value_string":
            bad = {"card": json.dumps(fields)}
        elif bad_shape == "column_string":
            bad = json.dumps({"card": fields})
        elif bad_shape == "wrong_key":
            bad = {"other": fields}
        elif bad_shape == "extra_field":
            bad = {"card": {**fields, "extra": True}}
        else:
            fields.pop("candidate_digest")
            bad = {"card": fields}
        async with plain_pool.acquire() as connection:
            await connection.execute(
                f"""UPDATE {plain_store.table}
                    SET prepared=($3::text)::jsonb, prepared_count=1
                    WHERE namespace=$1 AND transaction_id=$2""",
                plain_store.namespace, row.transaction_id, json.dumps(bad))
        for reader in (plain_store, codec_store):
            with pytest.raises(DecisionRefused, match="stored_receipt_invalid"):
                await reader.read(row.transaction_id)
    finally:
        await plain_pool.close()

@pytest.mark.asyncio
async def test_reserved_identity_and_same_connection_rollback(store):
    import asyncpg

    first = draft()
    async with store.pool.acquire() as connection:
        with pytest.raises(RuntimeError, match="rollback"):
            async with connection.transaction():
                row = await store.begin(first, transaction_id="tx-a", epoch=7,
                                        connection=connection)
                assert row.intent.digest == first.bind("tx-a", 7).digest
                assert (await store.read("tx-a", connection=connection)) == row
                raise RuntimeError("rollback")
    assert await store.read("tx-a") is None
    row = await store.begin(first, transaction_id="tx-a", epoch=7)
    assert (await store.begin(first, transaction_id="tx-a", epoch=7)) == row
    with pytest.raises(DecisionRefused, match="intent_conflict"):
        await store.begin(first, transaction_id="tx-b", epoch=8)
    with pytest.raises(DecisionRefused, match="identity_pair_required"):
        await store.begin(first, epoch=7)
    async with store.pool.acquire() as connection:
        with pytest.raises(DecisionRefused, match="caller_transaction_required"):
            await store.begin(draft(request_id="other"), connection=connection)


@pytest.mark.asyncio
async def test_v2_one_terminal_cas_and_recovery_finish(store):
    names = ("card", "business")
    realms = {name: Realm(store, name) for name in names}
    manager = Coordinator(store, realms, Verifier())
    row = await manager.prepare(draft(request_id="two", names=names))
    assert row.state == "prepared"
    results = await asyncio.gather(
        manager.decide(row.transaction_id, "committed", witness_digest="e" * 64),
        manager.decide(row.transaction_id, "aborted"), return_exceptions=True)
    assert sum(result.state in ("committed", "aborted") for result in results
               if not isinstance(result, Exception)) == 1
    terminal = await store.read(row.transaction_id)
    assert terminal.decided_at is not None
    assert terminal.state in ("committed", "aborted")
    if terminal.state == "committed":
        with pytest.raises(DecisionRefused, match="decision_conflict"):
            await manager.decide(row.transaction_id, "committed",
                                 witness_digest="f" * 64)
    finished = await Coordinator(store, realms, Verifier()).finish(row.transaction_id)
    assert set(finished.finished) == set(names)
    assert await manager.recover() == []
    assert all(realm.finishes == 1 for realm in realms.values())


@pytest.mark.asyncio
async def test_v2_expiry_and_witness_are_database_enforced(store):
    import time

    row = await store.begin(draft(request_id="expired", expires_at=int(time.time()) + 2))
    with pytest.raises(DecisionRefused, match="not_expired"):
        await store.abort_expired(row.transaction_id)
    await asyncio.sleep(2.1)
    with pytest.raises(DecisionRefused, match="late_preparation"):
        await store.record_prepared(await Realm(store, "card").prepare(row.transaction_id))
    assert (await store.abort_expired(row.transaction_id)).state == "aborted"
    with pytest.raises(DecisionRefused, match="decision_conflict"):
        await store.decide(row.transaction_id, "committed", witness_digest="e" * 64)


@pytest.mark.asyncio
async def test_expired_prepared_commit_has_distinct_refusal_and_preserves_identity(store):
    import time

    row = await store.begin(draft(request_id="late-commit",
                                  expires_at=int(time.time()) + 2))
    receipt = await Realm(store, "card").prepare(row.transaction_id)
    assert (await store.record_prepared(receipt)).state == "prepared"
    await asyncio.sleep(2.1)
    with pytest.raises(DecisionRefused, match="commit_expired"):
        await store.decide(row.transaction_id, "committed", witness_digest="e" * 64)
    same = await store.read(row.transaction_id)
    assert same.state == "prepared" and same.intent.digest == row.intent.digest
    assert (await store.abort_expired(row.transaction_id)).state == "aborted"


@pytest.mark.asyncio
async def test_v2_receipt_and_decision_respect_caller_transaction(store):
    row = await store.begin(draft(request_id="outer"),
                            transaction_id="outer-tx", epoch=11)
    receipt = await Realm(store, "card").prepare(row.transaction_id)
    async with store.pool.acquire() as connection:
        with pytest.raises(RuntimeError, match="rollback"):
            async with connection.transaction():
                prepared = await store.record_prepared(receipt, connection=connection)
                assert prepared.state == "prepared"
                decided = await store.decide(row.transaction_id, "committed",
                                             witness_digest="e" * 64,
                                             connection=connection)
                assert decided.state == "committed"
                raise RuntimeError("rollback")
    assert (await store.read(row.transaction_id)).state == "preparing"


@pytest.mark.asyncio
async def test_concurrent_receipt_merges_keep_both_participants(store):
    names = ("card", "business")
    for number in range(20):
        row = await store.begin(draft(request_id=f"pair-{number}", names=names))
        realms = {name: Realm(store, name) for name in names}
        receipts = await asyncio.gather(*(
            realm.prepare(row.transaction_id) for realm in realms.values()))
        prepared = await asyncio.gather(*(
            store.record_prepared(receipt) for receipt in receipts))
        assert set((await store.read(row.transaction_id)).prepared) == set(names)
        assert any(result.state == "prepared" for result in prepared)
        await store.decide(row.transaction_id, "aborted")
        receipts = await asyncio.gather(*(
            realm.finish(row.transaction_id, "aborted") for realm in realms.values()))
        await asyncio.gather(*(
            store.record_finished(receipt) for receipt in receipts))
        assert set((await store.read(row.transaction_id)).finished) == set(names)
