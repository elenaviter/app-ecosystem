"""Disposable PostgreSQL participant and fresh-process recovery test driver."""
from __future__ import annotations

import asyncio
import json
import os
import sys

from service_foundation.coordination.durable_decision_v2 import (
    Coordinator, DecisionRefused, PostgresDecisionStore, Receipt,
)
from service_foundation.coordination.durable_wire import (
    participant_projection, projection_digest,
)


async def install_codecs(connection):
    for name in ("json", "jsonb"):
        await connection.set_type_codec(name, schema="pg_catalog",
                                        encoder=json.dumps, decoder=json.loads)


class Realm:
    def __init__(self, store, *, fail_id=""):
        self.store = store
        self.fail_id = fail_id
        self.calls = []

    async def receipt(self, transaction_id, digest):
        record = await self.store.read(transaction_id)
        intent = record.intent
        return Receipt(transaction_id, intent.epoch, intent.digest, "realm",
                       projection_digest(intent, "realm"),
                       participant_projection(intent, "realm")["candidate_digest"], digest)

    async def prepare(self, transaction_id):
        return await self.receipt(transaction_id, "b" * 64)

    async def finish(self, transaction_id, decision):
        self.calls.append(transaction_id)
        if transaction_id == self.fail_id:
            raise RuntimeError("participant_unavailable")
        async with self.store.pool.acquire() as connection:
            await connection.execute(
                f"INSERT INTO {self.store.schema}.effects VALUES ($1,$2) "
                "ON CONFLICT (transaction_id) DO NOTHING", transaction_id, decision)
        return await self.receipt(transaction_id, "c" * 64)


class Verifier:
    async def prepared(self, record, receipt):
        if receipt.receipt_digest != "b" * 64:
            raise DecisionRefused("untrusted_receipt")

    async def finished(self, record, receipt):
        if receipt.receipt_digest != "c" * 64:
            raise DecisionRefused("untrusted_receipt")


async def main():
    import asyncpg

    schema, namespace, after, limit, codec = sys.argv[1:]
    pool = await asyncpg.create_pool(os.environ["SERVICE_FOUNDATION_TEST_POSTGRES_DSN"],
                                     min_size=1, max_size=2,
                                     init=install_codecs if codec == "on" else None)
    try:
        store = PostgresDecisionStore(pool, schema=schema, namespace=namespace)
        rows, cursor, more = await Coordinator(
            store, {"realm": Realm(store)}, Verifier()).recover_page(limit=int(limit), after=after)
        print(json.dumps({"ids": [row.transaction_id for row in rows],
                          "next_after": cursor, "has_more": more}))
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
