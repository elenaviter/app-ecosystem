# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Finish the transaction that owns a prepared invocation-policy marker.

A marker is never evidence of ABORT. The recorded intent selects the decision
authority; only that authority can decide, and a committed decision is always
FINISHed before another policy change may proceed.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Mapping

from service_foundation.coordination.durable_decision_log import DecisionRefused

from connection_hub.invocation_policy.models import InvocationPolicyChange
from connection_hub.invocation_policy.service import InvocationPolicyConflict

from ..issuer_gate import MAX_DECISION_SECONDS
from .card_participant import PARTICIPANT, LocalCardIntentSource
from .transaction_store import read_receipt


class CardPolicyMarkerSettler:
    """Resolve one exact Card decision within a wait window, then FINISH it.

    ``coordinator`` is the Hub's normal coordinator over its durable decision
    store and Card participant. A caller-staged transaction uses that caller's
    bound decision reader and participant; the Hub must not record its ABORT.
    Missing or unreachable authority is an undecided outcome, never a release.
    """

    def __init__(
        self, *, card_store: Any, coordinator: Any, callers: Mapping[str, Any],
        max_wait_seconds: float = MAX_DECISION_SECONDS, poll_seconds: float = 0.25,
    ) -> None:
        self._card_store = card_store
        self._coordinator = coordinator
        self._callers = dict(callers)
        self._intents = LocalCardIntentSource(card_store)
        if not 0 < max_wait_seconds <= MAX_DECISION_SECONDS or poll_seconds <= 0:
            raise ValueError("policy_marker_wait_invalid")
        self._max_wait_seconds = max_wait_seconds
        self._poll_seconds = poll_seconds

    async def __call__(self, change: InvocationPolicyChange, owner_subject: str) -> None:
        suffix = ":" + change.authority.key
        if not change.change_id.endswith(suffix):
            raise InvocationPolicyConflict("invocation_policy_earlier_change_undecided")
        member_id = change.change_id[:-len(suffix)]
        deadline = time.monotonic() + self._max_wait_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InvocationPolicyConflict("invocation_policy_earlier_change_undecided")
            try:
                # Decision lookup is bounded. Once COMMIT or ABORT is known,
                # FINISH is not governed by that clock; COMMIT must materialize.
                finish = await asyncio.wait_for(
                    self._terminal_once(change, owner_subject, member_id), timeout=remaining,
                )
            except Exception:  # noqa: BLE001 - an unavailable authority remains undecided
                finish = None
            if finish is not None:
                try:
                    await finish()
                except Exception as exc:
                    # A failed FINISH keeps its marker; the next request can
                    # replay the same durable decision without guessing.
                    raise InvocationPolicyConflict("invocation_policy_earlier_change_undecided") from exc
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InvocationPolicyConflict("invocation_policy_earlier_change_undecided")
            await asyncio.sleep(min(self._poll_seconds, remaining))

    async def _terminal_once(
        self, change: InvocationPolicyChange, owner_subject: str, member_id: str,
    ) -> Callable[[], Awaitable[Any]] | None:
        receipt = await read_receipt(self._card_store, member_id)
        if receipt is None or receipt.get("transaction_id") != member_id:
            return None
        # The policy marker must name this exact staged effect, not merely a
        # different transaction whose id happens to share the marker prefix.
        if not any(
            effect.get("kind") == "invocation_policy"
            and effect.get("key") == change.authority.key
            and isinstance(effect.get("payload"), Mapping)
            and effect["payload"].get("owner_subject") == owner_subject
            and effect["payload"].get("mode") == change.mode
            for effect in receipt.get("effects", ())
            if isinstance(effect, Mapping)
        ):
            return None
        group = receipt.get("group")
        transaction_id = group.get("transaction_id") if isinstance(group, Mapping) else member_id
        if not isinstance(transaction_id, str) or not transaction_id:
            return None
        intent = await self._intents.binding(transaction_id)
        if intent is None:
            return None
        if not intent.authority:
            record = await self._coordinator.store.read(transaction_id)
            if not self._matches(record, receipt):
                return None
            if not record.terminal:
                try:
                    record = await self._coordinator.store.abort_expired(transaction_id)
                except DecisionRefused as exc:
                    if str(exc) == "not_expired":
                        return None
                    raise
                if not self._matches(record, receipt) or not record.terminal:
                    return None
            return lambda: self._coordinator.finish(transaction_id)
        caller = self._callers.get(intent.authority)
        if caller is None:
            return None
        bound = caller.bind(intent.scope)
        record = await bound.decisions.read(transaction_id)
        if not self._matches(record, receipt) or not record.terminal:
            return None
        # This is the same participant FINISH used by the authenticated caller
        # endpoint. Its authority's decision is read, never written, here.
        return lambda: bound.participant.finish(transaction_id, record.state)

    @staticmethod
    def _matches(record: Any, receipt: Mapping[str, Any]) -> bool:
        return bool(
            record is not None
            and record.intent.digest == receipt.get("intent_digest")
            and PARTICIPANT in record.intent.participants
        )


__all__ = ["CardPolicyMarkerSettler"]
