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

from typing import Any, Mapping

from service_foundation.coordination.durable_decision_log import Coordinator, PostgresDecisionStore

from . import transaction_store as tx
from .card_participant import (
    PARTICIPANT, DecisionStorePort, HubCardParticipant, HubLocalReceiptVerifier, LocalCardIntentSource,
)
from .effect_targets import compose_card_effects

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
    """The kernel's one production store, in the Hub's own schema and namespace."""
    async with pg_pool.acquire() as connection:
        await connection.execute(f"CREATE SCHEMA IF NOT EXISTS {DECISION_SCHEMA}")
    store = PostgresDecisionStore(pg_pool, schema=DECISION_SCHEMA,
                                  namespace=decision_namespace(tenant=tenant, project=project))
    await store.ensure_schema()
    return store


def bind_card_transactions(service: Any, *, persistence: Any, decisions: Any, grant_store: Any,
                           policies: Any) -> Coordinator:
    """Bind one coordinator, participant, verifier and effect applier to this service's Card store."""
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
    coordinator = Coordinator(decisions, {PARTICIPANT: participant}, HubLocalReceiptVerifier(card_store))
    service.bind_card_coordinator(coordinator, intents=intents, decisions=decisions)
    return coordinator


__all__ = ["CardTransactionsUnavailable", "DECISION_SCHEMA", "bind_card_transactions", "card_transactions_enabled",
           "decision_namespace", "postgres_decision_store"]
