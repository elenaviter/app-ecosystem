# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Resident custody behind the existing credential_issue effect.

Only the stored plan selects this adapter. A provider receives a typed result
after the real decision and slot state have been read; caller objects never
establish that authority. FINISH keeps the Card fenced until the original
bearer is installed. Replaying after either activation or installation reads
the same SDK original, without preparing a replacement.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any, Mapping

from ..oauth_issuance import OAuthIssuancePlan, OAuthIssuanceResult, SlotOutcome
from .identity import CARD_KIND_AGENT
from .model import CardCredentialHandles
from .participant_effects import EffectBinding, ParticipantEffectRefused

RESIDENT_ISSUANCE_KIND = "descriptor-resident"


def _result(binding: EffectBinding, plan: OAuthIssuancePlan, *, outcome: str,
             token_sha256: str) -> OAuthIssuanceResult:
    committed = outcome != "released"
    return OAuthIssuanceResult(
        transaction_id=plan.transaction_id, intent_digest=plan.intent_digest,
        state="committed" if committed else "aborted", access_id=plan.access_id,
        card_revision=plan.candidate_revision if committed else plan.base_revision,
        expires_at=plan.expires_at, delivery_deadline=plan.delivery_deadline,
        receipt_digest=binding.receipt_digest if committed else "",
        # ABORT's retirement is stable even if a late, previously in-flight
        # reservation appears after the first cleanup saw no slot.
        per_slot={"access": SlotOutcome(outcome, plan.effect_digests["access"],
                                         token_sha256 if committed else "")})


class ResidentCredentialEffects:
    def __init__(self, *, store: Any, decisions: Any, provider: Any, handles: Any, read_card: Any) -> None:
        self._store, self._decisions = store, decisions
        self._provider, self._handles = provider, handles
        self._read_card = read_card

    async def context(self, binding: EffectBinding, payload: Mapping[str, Any], *,
                      state: str, live: Any = None) -> OAuthIssuancePlan | None:
        """Authenticate a resident plan against this exact local receipt and decision."""
        reader = getattr(self._store, "read_issuance_plan", None)
        if not callable(reader):
            if self._provider is not None:
                raise ParticipantEffectRefused("card_effect_adapter_unavailable")
            return None  # legacy OAuth targets need only their existing slot port
        receipt = binding.receipt()
        group = receipt.get("group")
        decision_id = group["transaction_id"] if isinstance(group, Mapping) else binding.transaction_id
        stored = await reader(decision_id)
        raw = stored.get("plan") if isinstance(stored, Mapping) else None
        if not isinstance(raw, Mapping) or raw.get("issuance_kind") != RESIDENT_ISSUANCE_KIND:
            return None
        if (self._decisions is None or self._handles is None
                or any(not callable(getattr(self._provider, name, None)) for name in ("activate", "retire"))):
            raise ParticipantEffectRefused("card_effect_adapter_unavailable")
        row = await self._decisions.read(decision_id)
        try:
            before, after = receipt.get("before"), receipt["after"]
            base_revision = 0 if before is None else before["card_revision"]
            valid = (stored["transaction_id"] == decision_id and row is not None
                and row.state in (("preparing", "prepared") if state == "prepared" else (state,))
                and row.intent.digest == receipt["intent_digest"]
                and raw["intent"]["card_kind"] == CARD_KIND_AGENT and raw["slots"] == ["access"]
                and payload["slot"] == "access" and raw["effect_digests"]["access"] == binding.effect_digest
                and raw["access_id"] == receipt["access_id"] == payload["access_id"]
                and raw["base_revision"] == base_revision
                and raw["candidate_revision"] == after["card_revision"] == payload["card_revision"]
                and raw["card_content_hash"] == after["content_hash"]
                and raw["expires_at"] == after["expires_at"] == payload["expires_at"]
                and raw["delivery_deadline"] == raw["expires_at"]
                and all(type(raw[name]) is int for name in ("base_revision", "candidate_revision",
                    "expires_at", "delivery_deadline", "reserved_until"))
                and raw["tenant"] == self._store.tenant and raw["project"] == self._store.project)
            if not valid:
                raise ValueError("binding")
            # Terminal retirement needs identity only. For activation the exact
            # committed live Card supplies the snapshot, as the ordinary plan reader does.
            snapshot = {"operations": [], "resource_grants": {}, "resource_operations": {}}
            if live is not None:
                if (live.card_kind != CARD_KIND_AGENT or live.access_id != raw["access_id"]
                        or live.content_hash() != raw["card_content_hash"]):
                    raise ValueError("target")
                snapshot = {"operations": list(live.operations),
                            "resource_grants": dict(live.resource_grants),
                            "resource_operations": dict(live.resource_operations)}
            return OAuthIssuancePlan.from_mapping({**raw, **snapshot, "transaction_id": decision_id,
                                                   "intent_digest": row.intent.digest})
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ParticipantEffectRefused("card_effect_binding_mismatch") from None

    async def finish(self, binding: EffectBinding, plan: OAuthIssuancePlan, *,
                     outcome: str, live: Any = None) -> str:
        """Install only an applied original, or retire an authenticated terminal one."""
        reservations = await self._store.issuance_reservations(plan.transaction_id)
        held = reservations.get("access", {})
        if ((outcome == "applied" and held.get("state") != "activated")
                or (outcome == "released" and held and held.get("state") not in ("released", "expired"))
                or (outcome == "superseded" and held.get("outcome") != "superseded")):
            raise ParticipantEffectRefused("card_effect_binding_mismatch")
        result = _result(binding, plan, outcome=outcome, token_sha256=held.get("token_sha256", ""))
        if outcome != "applied":
            await self._provider.retire(plan=plan, result=result)
            return outcome
        if live is None:
            if await self._store.issuance_clock() >= plan.expires_at:
                # The SQL slot may have activated before a custody cut. It
                # cannot be released or rewritten after that point. Its fixed
                # Card cap, the SDK original's fixed claim expiry and delivery
                # deadline have all ended. Complete the effect as superseded
                # without activating, signing, installing or trying to retire
                # a possibly activated SDK session. A merely moved live Card
                # does not qualify for this terminal lease case.
                return "superseded"
            raise ParticipantEffectRefused("card_effect_binding_mismatch")
        handles = await self._provider.activate(plan=plan, result=result)
        if (type(handles) is not CardCredentialHandles or handles.access_id != plan.access_id
                or not handles.access_token or handles.refresh_token or not handles.session_id
                or hashlib.sha256(handles.access_token.encode("utf-8")).hexdigest() != held.get("token_sha256")):
            raise ParticipantEffectRefused("card_effect_binding_mismatch")
        await self._handles.write(live, handles)
        return "applied"

    async def retire_previous(self, binding: EffectBinding, payload: Mapping[str, Any]) -> None:
        """Retire the old session only after new custody, or when no Card is live.

        FINISH executes recorded effects in order and stops on failure. This
        metadata fence also refuses an out-of-order cutoff after slot
        activation but before installation. A superseded new issuance may
        retire the old session only while its authoritative Card is no longer
        live; it never logs out a live Card's old session without new custody.
        """
        receipt = binding.receipt()
        issue = next((effect for effect in receipt["effects"]
            if effect["kind"] == "credential_issue" and effect["key"] == "access"
            and effect["payload"]["access_id"] == payload["access_id"]), None)
        if issue is None:
            return  # ordinary pre-existing grant_unbind, not resident issuance
        from ..oauth_issuance import effect_digest

        issue_binding = replace(binding, kind="credential_issue", key="access", effect_digest=effect_digest(issue))
        plan = await self.context(issue_binding, issue["payload"], state="committed")
        if plan is None:
            return
        retire = getattr(self._provider, "retire_previous", None)
        if not callable(retire):
            raise ParticipantEffectRefused("card_effect_adapter_unavailable")
        held = (await self._store.issuance_reservations(plan.transaction_id)).get("access", {})
        outcome = ("applied" if held.get("state") == "activated" else "superseded"
                   if held.get("state") == "released" and held.get("outcome") == "superseded" else "")
        if not outcome:
            raise ParticipantEffectRefused("card_effect_binding_mismatch")
        current = await self._read_card(issue_binding, issue["payload"])
        now = await self._store.issuance_clock()
        if current is not None and current.state == "active" and current.expires_at > now:
            if outcome != "applied":
                raise ParticipantEffectRefused("card_effect_binding_mismatch")
            read_binding = getattr(self._handles, "binding_identity", None)
            if not callable(read_binding):
                raise ParticipantEffectRefused("card_effect_adapter_unavailable")
            installed = await read_binding(plan.access_id)
            if (not isinstance(installed, Mapping) or installed.get("from_fingerprint") != held.get("token_sha256")
                    or installed.get("from_revision") != plan.candidate_revision
                    or installed.get("from_expires_at") != plan.expires_at):
                raise ParticipantEffectRefused("card_effect_binding_mismatch")
        await retire(plan=plan, result=_result(issue_binding, plan, outcome=outcome,
            token_sha256=held.get("token_sha256", "")),
            session_id=payload["session_id"], token_sha256=payload["token_sha256"])


__all__ = ["RESIDENT_ISSUANCE_KIND", "ResidentCredentialEffects"]
