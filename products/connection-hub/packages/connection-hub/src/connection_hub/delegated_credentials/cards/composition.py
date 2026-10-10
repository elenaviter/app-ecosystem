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
from .participant_operation import RoutedDecisionPort

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


def card_transaction_coordinator(*, persistence: Any, decisions: Any, grant_store: Any, policies: Any,
                                 authorities: Mapping[str, Any] | None = None, catalog_store: Any = None,
                                 accounts_for: Any = None, issuance_store: Any = None,
                                 credential_handles: Any = None,
                                 ) -> tuple[Coordinator, LocalCardIntentSource]:
    """One coordinator, participant, verifier and effect applier over this persistence's Card store.

    ``authorities`` maps each configured participant caller to its per-scope
    authority reader: a Card staged by that caller's transaction resolves its
    decision there, one the Hub staged itself reads ``decisions``.
    ``catalog_store`` lets a transaction reserve the active catalog version
    (``catalog-active:`` dependency); without it such a transaction is refused.
    ``issuance_store`` (the PostgreSQL OAuth authority) and ``credential_handles``
    (the Card's handle store) bind W603's ``credential_issue`` target; without
    the store that effect refuses ``card_effect_adapter_unavailable``.
    """
    if persistence is None or decisions is None:
        raise CardTransactionsUnavailable("card_transactions_unavailable")
    card_store = getattr(persistence, "card_store", None)
    card_service = getattr(persistence, "card_service", None)
    if card_store is None or card_service is None:
        raise CardTransactionsUnavailable("card_transactions_unavailable")
    tx.bind_transaction_decisions(card_store, RoutedDecisionPort(local=decisions, card_store=card_store,
                                                                 authorities=authorities or {}))
    if catalog_store is not None:
        from ..catalog.reservations import CatalogReservations
        tx.bind_catalog_reservations(card_store, CatalogReservations(catalog_store))
    compose_card_effects(card_service=card_service, card_store=card_store, grant_store=grant_store,
                         policies=policies, accounts_for=accounts_for, issuance_store=issuance_store,
                         credential_handles=credential_handles)
    intents = LocalCardIntentSource(card_store, service=card_service, decisions=decisions)
    participant = HubCardParticipant(service=card_service, store=card_store, intents=intents, decisions=decisions)
    return Coordinator(decisions, {PARTICIPANT: participant}, HubLocalReceiptVerifier(card_store)), intents


def bind_card_transactions(service: Any, *, persistence: Any, decisions: Any, grant_store: Any,
                           policies: Any, authorities: Mapping[str, Any] | None = None,
                           catalog_store: Any = None, accounts_for: Any = None,
                           managed_control_scopes: Any = (), issuance_store: Any = None,
                           credential_handles: Any = None) -> Coordinator:
    """Bind one coordinator, participant, verifier and effect applier to this service's Card store.

    ``accounts_for(grantor)`` (W578) is the grantor's connected-account store
    composed with the shared account lock; without it an account disconnect
    under Card transactions refuses as unavailable. ``managed_control_scopes``
    are the configured callers' plan scopes: a project's Control Card there is
    written only through that caller's transaction. ``issuance_store`` (W603)
    also binds the service's original OAuth issuance; without it
    ``begin_oauth_issuance`` refuses ``card_transactions_unavailable``.
    """
    coordinator, intents = card_transaction_coordinator(persistence=persistence, decisions=decisions,
                                                        grant_store=grant_store, policies=policies,
                                                        authorities=authorities, catalog_store=catalog_store,
                                                        accounts_for=accounts_for, issuance_store=issuance_store,
                                                        credential_handles=credential_handles)
    service.bind_card_coordinator(coordinator, intents=intents, decisions=decisions)
    if callable(getattr(service, "bind_managed_control_scopes", None)):
        service.bind_managed_control_scopes(managed_control_scopes)
    if accounts_for is not None and callable(getattr(service, "bind_account_stores", None)):
        service.bind_account_stores(accounts_for)
    if issuance_store is not None and callable(getattr(service, "bind_oauth_issuance_store", None)):
        service.bind_oauth_issuance_store(issuance_store)
    if credential_handles is not None and callable(getattr(service, "bind_card_credential_handles", None)):
        # W606: the writer pins each edited Card's handle row so its COMMIT moves the binding.
        service.bind_card_credential_handles(credential_handles)
    return coordinator


async def recover_card_transactions(coordinator: Coordinator, *, limit: int = 100, after: str = "",
                                    max_pages: int = 5) -> dict[str, Any]:
    """One bounded recovery pass of at most ``max_pages`` pages from cursor ``after``.

    Finishes every decided transaction on every participant, presumes ABORT
    for an undecided one past its expiry (the store's own CAS), and leaves an
    unexpired undecided one. A participant failure does not stop the pass: it
    is logged by transaction id and refusal code, the cursor moves past it, and
    it is retried after the cursor wraps (EMain 18:42). The result's
    ``next_after`` is where the next pass starts; it is "" once the last page
    has been read, so every row is revisited.
    """
    from service_foundation.coordination.durable_decision_log import DecisionRefused, RecoveryIncomplete

    cursor = after if type(after) is str and len(after) <= 128 else ""
    finished = pending = 0
    failed: dict[str, Any] = {}
    pages = 0
    while pages < max_pages:
        pages += 1
        try:
            records, next_after, has_more = await coordinator.recover_page(limit=limit, after=cursor)
            page_failures = {}
        except RecoveryIncomplete as exc:
            next_after, has_more = getattr(exc, "next_after", None), getattr(exc, "has_more", None)
            if type(next_after) is not str or type(has_more) is not bool:
                raise  # a kernel without the paging contract: never guess a cursor
            records, page_failures = list(exc.completed), exc.failures
        for record in records:
            if record.terminal and set(record.finished) == set(record.intent.participants):
                finished += 1
            else:
                pending += 1
        failed.update(page_failures)
        if not has_more:
            cursor = ""
            break
        cursor = next_after
    for transaction_id, error in failed.items():
        reason = str(error) if isinstance(error, DecisionRefused) else type(error).__name__
        LOGGER.warning("[connection-hub.card-transactions] recovery failed transaction=%s reason=%s",
                       transaction_id, reason)
    return {"ok": not failed, "finished": finished, "pending": pending, "failed": len(failed),
            "pages": pages, "next_after": cursor}


__all__ = ["CardTransactionsUnavailable", "DECISION_SCHEMA", "bind_card_transactions", "card_transaction_coordinator",
           "card_transactions_enabled", "decision_namespace", "postgres_decision_store", "recover_card_transactions"]
