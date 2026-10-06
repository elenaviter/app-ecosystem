# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W502: compose the Hub's Card writes into the ONE generalized transaction protocol (W581 v2).

When enabled, an existing-Card write the Hub initiates runs as one
transaction through the kernel ``Coordinator`` with the kernel's
``PostgresDecisionStore``, the Hub's ``HubCardParticipant`` and
``HubLocalReceiptVerifier``; its effects are applied only after COMMIT by
``compose_card_effects`` (W582). Nothing here is module state: the hosting
entrypoint calls ``bind_card_transactions`` for each service it builds, with
the decision store it owns.

Activation is a separate, approved step. ``card_transactions_enabled`` reads
``connections.card_transactions.enabled`` and is False unless it is exactly
``true``, so merging this source changes no live behaviour. When it is true
but the PostgreSQL authority or Card storage is missing, composition refuses
(``card_transactions_unavailable``) rather than silently writing directly.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from service_foundation.coordination.durable_decision_log import Coordinator, PostgresDecisionStore

from . import transaction_store as tx
from .card_participant import (
    PARTICIPANT, DecisionStorePort, HubCardParticipant, HubLocalReceiptVerifier, LocalCardIntentSource,
)
from .effect_targets import compose_card_effects

LOGGER = logging.getLogger("kdcube.connection_hub.card_transactions")
DECISION_SCHEMA = "connection_hub_card_decisions"


class CardTransactionsUnavailable(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def card_transactions_enabled(connections: Mapping[str, Any] | None) -> bool:
    section = (connections or {}).get("card_transactions") if isinstance(connections, Mapping) else None
    return isinstance(section, Mapping) and section.get("enabled") is True


def decision_namespace(*, tenant: str, project: str) -> str:
    return f"{PARTICIPANT}:{tenant}:{project}"


async def postgres_decision_store(pg_pool: Any, *, tenant: str, project: str) -> PostgresDecisionStore:
    """The kernel's one production store, in the Hub's own schema and namespace.

    Concurrent ``CREATE ... IF NOT EXISTS`` can fail in PostgreSQL (EMain #599
    N1), so first creation runs under a session advisory lock: one creator at
    a time across processes.
    """
    namespace = decision_namespace(tenant=tenant, project=project)
    async with pg_pool.acquire() as connection:
        await connection.execute("SELECT pg_advisory_lock(hashtext($1))", DECISION_SCHEMA)
        try:
            await connection.execute(f"CREATE SCHEMA IF NOT EXISTS {DECISION_SCHEMA}")
            # Schema DDL on the SAME connection that holds the lock, so a pool
            # of size 1 cannot deadlock waiting for a second one (EMain #599).
            await PostgresDecisionStore(_HeldConnection(connection), schema=DECISION_SCHEMA,
                                        namespace=namespace).ensure_schema()
        finally:
            await connection.execute("SELECT pg_advisory_unlock(hashtext($1))", DECISION_SCHEMA)
    return PostgresDecisionStore(pg_pool, schema=DECISION_SCHEMA, namespace=namespace)


class _HeldConnection:
    """A pool-shaped view of one already-acquired connection, for schema setup only."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def acquire(self) -> Any:
        connection = self._connection

        class _Lease:
            async def __aenter__(self):
                return connection

            async def __aexit__(self, *exc):
                return False

        return _Lease()


def card_transaction_coordinator(*, persistence: Any, decisions: Any, grant_store: Any,
                                 policies: Any) -> tuple[Coordinator, LocalCardIntentSource]:
    """One coordinator, participant, verifier and effect applier over this persistence's Card store."""
    if persistence is None or decisions is None:
        raise CardTransactionsUnavailable("card_transactions_unavailable")
    card_store = getattr(persistence, "card_store", None)
    card_service = getattr(persistence, "card_service", None)
    if card_store is None or card_service is None:
        raise CardTransactionsUnavailable("card_transactions_unavailable")
    tx.bind_transaction_decisions(card_store, DecisionStorePort(decisions))
    compose_card_effects(card_service=card_service, card_store=card_store, grant_store=grant_store,
                         policies=policies)
    intents = LocalCardIntentSource(card_store)
    participant = HubCardParticipant(service=card_service, store=card_store, intents=intents, decisions=decisions)
    return Coordinator(decisions, {PARTICIPANT: participant}, HubLocalReceiptVerifier(card_store)), intents


def bind_card_transactions(service: Any, *, persistence: Any, decisions: Any, grant_store: Any,
                           policies: Any) -> Coordinator:
    """Bind one coordinator, participant, verifier and effect applier to this service's Card store."""
    coordinator, intents = card_transaction_coordinator(persistence=persistence, decisions=decisions,
                                                        grant_store=grant_store, policies=policies)
    service.bind_card_coordinator(coordinator, intents=intents, decisions=decisions)
    return coordinator


async def recover_card_transactions(coordinator: Coordinator, *, limit: int = 100) -> dict[str, Any]:
    """One bounded recovery pass (EMain #599: the activation gate).

    Finishes every decided transaction on every participant, presumes ABORT
    for an undecided one past its expiry (the store's own CAS), and leaves an
    unexpired undecided one. A participant failure does not stop the pass; it
    is logged by transaction id and refusal code and retried next pass. A
    backlog larger than ``limit`` is a named result, never an exception
    (EMain #601); draining it page by page needs the kernel's paged read.
    """
    from service_foundation.coordination.durable_decision_log import DecisionRefused, RecoveryIncomplete

    try:
        records = await coordinator.recover(limit=limit)
    except RecoveryIncomplete as exc:
        records, failed = list(exc.completed), exc.failures
    except DecisionRefused as exc:
        if str(exc) != "recovery_unbounded":
            raise
        LOGGER.warning("[connection-hub.card-transactions] recovery backlog exceeds limit=%s", limit)
        return {"ok": False, "reason": "recovery_unbounded", "finished": 0, "pending": 0, "failed": 0}
    else:
        failed = {}
    for transaction_id, error in failed.items():
        reason = str(error) if isinstance(error, DecisionRefused) else type(error).__name__
        LOGGER.warning("[connection-hub.card-transactions] recovery failed transaction=%s reason=%s",
                       transaction_id, reason)
    finished = sum(1 for record in records if record.terminal and set(record.finished) == set(record.intent.participants))
    return {"ok": not failed, "finished": finished, "pending": len(records) - finished, "failed": len(failed)}


__all__ = ["CardTransactionsUnavailable", "DECISION_SCHEMA", "bind_card_transactions", "card_transaction_coordinator",
           "card_transactions_enabled", "decision_namespace", "postgres_decision_store", "recover_card_transactions"]
