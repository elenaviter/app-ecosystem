# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W582: the Hub's concrete targets for Card transaction effects, and their composition.

``participant_effects.ParticipantEffectApplier`` validates every effect against
the exact committed (or prepared, or aborted) receipt and dispatches it here.
Each target is an idempotent, revision-bound operation that returns the
binding's effect digest after an application or an identical prior one, so a
replay after a crash before FINISH writes its marker lands on the same state:

- ``credential_lifetime`` -> ``set_card_credentials_expiry``: one ABSOLUTE
  deadline, written only to LIVE credentials (an ended one is never revived)
  and not rewritten when already there. ``no_active_credentials`` is the named,
  replay-stable no-op the applier accepts for this kind only.
- ``invocation_policy`` -> the policy service's prepared change, keyed by
  ``<transaction_id>:<key>``: STAGE prepares it (fail-closed marker), COMMIT
  publishes it, a recorded ABORT releases only that exact prepared marker.
- ``grant_unbind`` -> ``revoke_access_grant_by_digest``: the old bearer's
  binding is removed by its pinned SHA-256; the raw token is never held.

``grant_binding`` has no target: the helper refuses it until the SDK's
no-second-mint custody qualifies (Ops C1). Nothing here is module state: the
composition root builds one applier per Card service it binds.
"""

from __future__ import annotations

from typing import Any, Mapping

from connection_hub.invocation_policy.models import InvocationAuthority
from connection_hub.invocation_policy.service import InvocationPolicyConflict

from . import transaction_store as tx
from .participant_effects import EffectBinding, ParticipantEffectApplier, ParticipantEffectRefused

NO_ACTIVE_CREDENTIALS = "no_active_credentials"


def _change_id(binding: EffectBinding) -> str:
    # The helper validated exactly this identity (tx:key) for the policy kind.
    return f"{binding.transaction_id}:{binding.key}"


class CredentialLifetimeTarget:
    """Absolute expiry for a Card's live OAuth credentials (SQL authority).

    The deadline is written with the committed Card revision (the receipt's
    ``after`` revision), so the credential family's cap never moves back to an
    older revision's deadline and rotation is bounded by it (W585).

    Outcome stability (Ops F-b): the outcome is the target's answer at apply
    time. If a crash falls between the target write and FINISH's marker, and
    the credentials end in between, the replay records ``no_active_credentials``
    although the deadline had been applied. The deadline itself is never
    revived or extended either way; only the recorded outcome differs.
    """

    def __init__(self, grant_store: Any) -> None:
        self._grant_store = grant_store

    async def apply_once(self, binding: EffectBinding, payload: Mapping[str, Any]) -> str:
        set_expiry = getattr(self._grant_store, "set_card_credentials_expiry", None)
        if set_expiry is None:
            raise ParticipantEffectRefused("card_effect_adapter_unavailable")
        committed_revision = int(binding.receipt()["after"]["card_revision"])
        outcome = await set_expiry(payload["access_id"], payload["expires_at"], card_revision=committed_revision)
        if outcome == "applied":
            return binding.effect_digest
        if outcome == NO_ACTIVE_CREDENTIALS:
            return NO_ACTIVE_CREDENTIALS
        raise ParticipantEffectRefused("card_effect_binding_mismatch")

    async def prepare_once(self, binding: EffectBinding, payload: Mapping[str, Any]) -> str:
        return binding.effect_digest  # nothing to prepare: the deadline moves only after COMMIT

    async def release_once(self, binding: EffectBinding, payload: Mapping[str, Any]) -> str:
        return binding.effect_digest


class InvocationPolicyTarget:
    """The binding's invocation policy, prepared at STAGE and published at COMMIT."""

    def __init__(self, policies: Any) -> None:
        self._policies = policies

    @staticmethod
    def _authority(payload: Mapping[str, Any]) -> InvocationAuthority:
        return InvocationAuthority.from_mapping(payload["authority"])

    async def prepare_once(self, binding: EffectBinding, payload: Mapping[str, Any]) -> str:
        try:
            await self._policies.prepare_policy_change(
                owner_subject=payload["owner_subject"], authority=self._authority(payload),
                mode=payload["mode"], change_id=_change_id(binding),
                expected_revision=payload["expected_revision"])
        except InvocationPolicyConflict as exc:
            moved = getattr(exc, "reason", str(exc)) == "invocation_policy_revision_moved"
            raise ParticipantEffectRefused(
                "card_effect_target_revision_moved" if moved else "card_effect_policy_conflict") from None
        return binding.effect_digest

    async def apply_once(self, binding: EffectBinding, payload: Mapping[str, Any]) -> str:
        try:
            # Replay-safe: a change already committed by this id returns the
            # same policy; any other state of the record is a conflict.
            await self._policies.commit_policy_change(
                owner_subject=payload["owner_subject"], authority=self._authority(payload),
                change_id=_change_id(binding))
        except InvocationPolicyConflict:
            raise ParticipantEffectRefused("card_effect_policy_conflict") from None
        return binding.effect_digest

    async def release_once(self, binding: EffectBinding, payload: Mapping[str, Any]) -> str:
        # Removes only this exact PREPARED marker; a repeat, or a marker that
        # is gone or committed, is left as it is (idempotent).
        await self._policies.release_policy_change(
            owner_subject=payload["owner_subject"], authority=self._authority(payload),
            change_id=_change_id(binding))
        return binding.effect_digest


class GrantUnbindTarget:
    """Remove the old bearer's binding by its pinned digest."""

    def __init__(self, grant_store: Any) -> None:
        self._grant_store = grant_store

    async def apply_once(self, binding: EffectBinding, payload: Mapping[str, Any]) -> str:
        revoke = getattr(self._grant_store, "revoke_access_grant_by_digest", None)
        if revoke is None:
            raise ParticipantEffectRefused("card_effect_adapter_unavailable")
        outcome = await revoke(payload["token_sha256"])
        if outcome not in ("revoked", "absent", "unbound"):
            raise ParticipantEffectRefused("card_effect_binding_mismatch")
        return binding.effect_digest

    async def prepare_once(self, binding: EffectBinding, payload: Mapping[str, Any]) -> str:
        return binding.effect_digest

    async def release_once(self, binding: EffectBinding, payload: Mapping[str, Any]) -> str:
        return binding.effect_digest


class AccountDeleteTarget:
    """W578: a disconnect's deletion of exactly the account incarnation its decision named.

    ``accounts_for(grantor_subject)`` is that grantor's connected-account store.
    Applied only after COMMIT. A reconnection of the same deterministic account
    id is another incarnation and is left as it is (``account_reconnected``); an
    account already gone is ``account_absent``. Every outcome is final, so a
    replay after a crash before FINISH lands on the same state.
    """

    def __init__(self, accounts_for: Any) -> None:
        self._accounts_for = accounts_for

    async def apply_once(self, binding: EffectBinding, payload: Mapping[str, Any]) -> str:
        if self._accounts_for is None:
            raise ParticipantEffectRefused("card_effect_adapter_unavailable")
        accounts = self._accounts_for(payload["grantor_subject"])
        disconnect = getattr(accounts, "disconnect_incarnation", None)
        if disconnect is None:
            raise ParticipantEffectRefused("card_effect_adapter_unavailable")
        # The effect's own identity pins the first completed outcome; a replay returns it.
        outcome = await disconnect(payload["account_id"], payload["incarnation"], pin=binding.effect_digest)
        if outcome not in ("disconnected", "account_absent", "account_reconnected"):
            raise ParticipantEffectRefused("card_effect_binding_mismatch")
        return binding.effect_digest

    async def prepare_once(self, binding: EffectBinding, payload: Mapping[str, Any]) -> str:
        return binding.effect_digest  # nothing to prepare: the account goes only after COMMIT

    async def release_once(self, binding: EffectBinding, payload: Mapping[str, Any]) -> str:
        return binding.effect_digest


def compose_card_effects(*, card_service: Any, card_store: Any, grant_store: Any,
                         policies: Any, accounts_for: Any = None) -> ParticipantEffectApplier:
    """Bind one validated applier to a Card service's effect hooks.

    The receipt reader is the Card store's own durable receipt; the targets are
    the ones above. ``grant_binding`` stays refused by the applier.
    """

    async def read_receipt(transaction_id: str) -> Mapping[str, Any] | None:
        return await tx.read_receipt(card_store, transaction_id)

    applier = ParticipantEffectApplier(read_receipt=read_receipt, targets={
        "credential_lifetime": CredentialLifetimeTarget(grant_store),
        "invocation_policy": InvocationPolicyTarget(policies),
        "grant_unbind": GrantUnbindTarget(grant_store),
        "account_delete": AccountDeleteTarget(accounts_for),
    })
    card_service.bind_effect_applier(applier.apply)
    card_service.bind_effect_preparer(applier.prepare)
    card_service.bind_effect_releaser(applier.release)
    return applier


__all__ = ["AccountDeleteTarget", "CredentialLifetimeTarget", "GrantUnbindTarget", "InvocationPolicyTarget",
           "NO_ACTIVE_CREDENTIALS", "compose_card_effects"]
