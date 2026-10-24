# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Finish the transaction that owns an invocation-policy marker.

A marker is never evidence of ABORT. The recorded intent selects the decision
authority; only that authority can decide, and a committed decision is always
FINISHed before another policy change may proceed.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping

from service_foundation.coordination.durable_decision_log import DecisionRefused

from connection_hub.invocation_policy.models import (
    InvocationPolicyChange, POLICY_CHANGE_COMMITTED, POLICY_CHANGE_PREPARED,
)
from connection_hub.invocation_policy.store import owner_hash_for
from connection_hub.invocation_policy.service import (
    InvocationPolicyConflict,
    recovery_transaction_ref,
)

from ..issuer_gate import MAX_DECISION_SECONDS
from .card_participant import PARTICIPANT, LocalCardIntentSource
from .transaction_store import (
    card_version_outcome, card_version_txn_id, effect_outcomes, pending_effects,
    read_card_version_marker, read_receipt,
)

CARD_VERSION_PROOF_LIMIT = 64
# A long-lived Card (an agent's, renewed every hour) keeps far more than 256 revision files. The proof
# still reads only the newest 64 by number; this bounds the names-only directory scan that finds them.
CARD_VERSION_HISTORY_NAMES = 4096


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

    @staticmethod
    def _policy_effect_index(
        receipt: Mapping[str, Any], change: InvocationPolicyChange, owner_subject: str,
    ) -> int | None:
        for index, effect in enumerate(receipt.get("effects", ())):
            if not isinstance(effect, Mapping) or not isinstance(effect.get("payload"), Mapping):
                continue
            payload = effect["payload"]
            if (effect.get("kind") == "invocation_policy"
                    and effect.get("key") == change.authority.key
                    and payload.get("owner_subject") == owner_subject
                    and payload.get("authority") == change.authority.to_dict()
                    and payload.get("mode") == change.mode
                    and type(payload.get("expected_revision")) is int
                    and payload["expected_revision"] == change.expected_policy_revision):
                return index
        return None

    async def _finished_receipt(
        self, change: InvocationPolicyChange, owner_subject: str, member_id: str,
        *, state: str,
    ) -> bool:
        """Verify the exact effect and the terminal intent's durable FINISH witness."""
        receipt = await read_receipt(self._card_store, member_id)
        if receipt is None or receipt.get("state") != state:
            return False
        effect_index = self._policy_effect_index(receipt, change, owner_subject)
        if effect_index is None:
            return False
        group = receipt.get("group")
        decision_id = group.get("transaction_id") if isinstance(group, Mapping) else member_id
        terminal = receipt if decision_id == member_id else await read_receipt(self._card_store, decision_id)
        if (terminal is None or terminal.get("state") != state
                or terminal.get("intent_finished") is not True
                or terminal.get("intent_digest") != receipt.get("intent_digest")
                or terminal.get("participant") != PARTICIPANT):
            return False
        if state == "aborted":
            # ABORT can release only a PREPARED marker. A COMMITTED marker
            # requires COMMIT's exact applied effect and FINISH witness.
            return change.state == POLICY_CHANGE_PREPARED
        if await pending_effects(self._card_store, receipt):
            return False
        outcome = (await effect_outcomes(self._card_store, member_id)).get(str(effect_index))
        return isinstance(outcome, str) and bool(outcome)

    async def _finished_card_version(
        self, change: InvocationPolicyChange, owner_subject: str, txn: str,
    ) -> bool:
        """D2: the exact version survives only a fully completed PUBLISH.

        The committed policy marker is the policy-side effect witness. It is
        insufficient alone: derive OUTCOME's exact link, time and binding from
        that Card's immutable version, never a request or a name alone.
        Follow at most 64 immutable base links. A broken historical chain may
        use a bounded, one-Card names inventory (CARD_VERSION_HISTORY_NAMES directory entries).
        PREPARED, an in-progress txn or missing/over-bound evidence stays undecided.
        The policy service rechecks the marker and policy under its own lock.
        A change id can be request-supplied: retiring an already-applied
        COMMITTED marker applies no undecided effect, even for a forged id.
        """
        if change.state != POLICY_CHANGE_COMMITTED:
            return False
        card_version_txn_id(txn)
        store = self._card_store
        subject_hash, access_id = owner_hash_for(owner_subject), change.authority.access_id
        current = await store.read_current(subject_hash=subject_hash, access_id=access_id)
        if current is None or await read_card_version_marker(store, txn) is not None:
            return False
        name, seen, record = current.revision_name, set(), None
        for _ in range(CARD_VERSION_PROOF_LIMIT + 1):
            if not isinstance(name, str) or not name or name in seen:
                return False
            seen.add(name)
            record = await store.read_version_record(
                subject_hash=subject_hash, access_id=access_id, revision_name=name,
            )
            if isinstance(record, Mapping) and record.get("txn") == txn:
                break
            if not isinstance(record, Mapping) or not record.get("base"):
                # OAuth renewals before predecessor metadata was preserved
                # broke this chain. No project census or all-Card scan: names
                # from this one owner/access only, bounded before sorting.
                candidates = await store.policy_recovery_candidates(
                    subject_hash=subject_hash, access_id=access_id,
                    current_revision=current.card_revision,
                    max_predecessors=CARD_VERSION_PROOF_LIMIT, max_entries=CARD_VERSION_HISTORY_NAMES,
                )
                for candidate in candidates:
                    record = await store.read_version_record(
                        subject_hash=subject_hash, access_id=access_id, revision_name=candidate,
                    )
                    if isinstance(record, Mapping) and record.get("txn") == txn:
                        name = candidate
                        break
                else:
                    return False
                break
            name = record["base"]
        else:
            return False
        card = await store.read_revision(
            subject_hash=subject_hash, access_id=access_id, revision_name=name,
        )
        if (card is None or card.access_id != access_id or card.grantor_subject != owner_subject
                or card.card_revision > current.card_revision
                or (name == current.revision_name and (card.card_revision != current.card_revision
                                                       or card.content_hash() != current.content_hash))):
            return False
        at = datetime.fromisoformat(record["at"])
        result = await card_version_outcome(
            store, txn=txn, binding=record.get("binding"), at=at,
            links=[{"subject_hash": subject_hash, "access_id": access_id,
                    "version": card.card_revision, "checksum": card.content_hash()}],
        )
        from .card_version_reservation import read_reservation
        held = await read_reservation(store, subject_hash=subject_hash, access_id=access_id)
        return (result["state"] == "published"
                and (held is None or held["txn"] != txn)
                and await read_card_version_marker(store, txn) is None
                and await store.read_current(subject_hash=subject_hash, access_id=access_id) == current)

    async def __call__(self, change: InvocationPolicyChange, owner_subject: str) -> bool:
        suffix = ":" + change.authority.key
        recovery = recovery_transaction_ref(change)
        if change.change_id.endswith(suffix) and recovery is None:
            # CardVersion uses a separate transaction namespace and completion
            # protocol, not legacy Card receipts or a caller's FINISH endpoint.
            txn = change.change_id[:-len(suffix)]
            try:
                if await asyncio.wait_for(
                    self._finished_card_version(change, owner_subject, txn),
                    timeout=self._max_wait_seconds,
                ):
                    return True
            except Exception:  # noqa: BLE001 - incomplete/corrupt proof never clears a marker
                pass
        if not change.change_id.endswith(suffix) or recovery is None:
            raise InvocationPolicyConflict(
                "invocation_policy_earlier_change_undecided", transaction_ref=recovery,
            )
        member_id = change.change_id[:-len(suffix)]
        deadline = time.monotonic() + self._max_wait_seconds
        try:
            if await asyncio.wait_for(
                self._finished_receipt(change, owner_subject, member_id, state="committed"),
                timeout=max(0, deadline - time.monotonic()),
            ):
                return True
        except Exception:  # noqa: BLE001 - missing/corrupt evidence cannot clear the marker
            pass
        if recovery is not None:
            # A group member's marker names its member receipt. Recovery must
            # also name the group's distinct durable decision identity.
            try:
                receipt = await asyncio.wait_for(
                    read_receipt(self._card_store, member_id),
                    timeout=max(0, deadline - time.monotonic()),
                )
            except Exception:  # noqa: BLE001 - retain the exact member identity
                receipt = None
            if isinstance(receipt, Mapping) and receipt.get("transaction_id") == member_id:
                group = receipt.get("group")
                decision_id = group.get("transaction_id") if isinstance(group, Mapping) else member_id
                if (
                    isinstance(decision_id, str)
                    and len(decision_id) == 64
                    and all(char in "0123456789abcdef" for char in decision_id)
                ):
                    recovery["decision_transaction_id"] = decision_id
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InvocationPolicyConflict(
                    "invocation_policy_earlier_change_undecided", transaction_ref=recovery,
                )
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
                    # FINISH may replay after the policy was committed but
                    # before its Card effect was acknowledged. Require the
                    # durable outcome before the caller retires the marker.
                    if not (await asyncio.wait_for(
                        self._finished_receipt(change, owner_subject, member_id, state="committed"),
                        timeout=MAX_DECISION_SECONDS,
                    ) or await asyncio.wait_for(
                        self._finished_receipt(change, owner_subject, member_id, state="aborted"),
                        timeout=MAX_DECISION_SECONDS,
                    )):
                        raise ValueError("card_effect_unverified")
                except Exception as exc:
                    # A failed FINISH keeps its marker; the next request can
                    # replay the same durable decision without guessing.
                    raise InvocationPolicyConflict(
                        "invocation_policy_earlier_change_undecided", transaction_ref=recovery,
                    ) from exc
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InvocationPolicyConflict(
                    "invocation_policy_earlier_change_undecided", transaction_ref=recovery,
                )
            await asyncio.sleep(min(self._poll_seconds, remaining))

    async def _terminal_once(
        self, change: InvocationPolicyChange, owner_subject: str, member_id: str,
    ) -> Callable[[], Awaitable[Any]] | None:
        receipt = await read_receipt(self._card_store, member_id)
        if receipt is None or receipt.get("transaction_id") != member_id:
            return None
        # The policy marker must name this exact staged effect, not merely a
        # different transaction whose id happens to share the marker prefix.
        if self._policy_effect_index(receipt, change, owner_subject) is None:
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
