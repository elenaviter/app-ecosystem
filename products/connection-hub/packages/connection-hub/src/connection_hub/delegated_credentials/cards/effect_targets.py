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

- ``credential_issue`` -> the OAuth authority's reservation of an original
  issuance (W603): STAGE binds the reservation to this effect, COMMIT
  activates it only while the authoritative Card is still exactly the
  committed revision, live and unexpired (otherwise ``superseded``, creating
  nothing), and a recorded ABORT releases it.

``grant_binding`` has no target: the helper refuses it until the SDK's
no-second-mint custody qualifies (Ops C1). Nothing here is module state: the
composition root builds one applier per Card service it binds.
"""

from __future__ import annotations

from typing import Any, Mapping

from connection_hub.invocation_policy.models import InvocationAuthority
from connection_hub.invocation_policy.service import InvocationPolicyConflict

from ..durable_io import read_json_or_none
from . import transaction_store as tx
from .model import CARD_STATE_ACTIVE, CardCurrentPointer
from .participant_effects import (
    CREDENTIAL_ISSUE_SUPERSEDED, EffectBinding, ParticipantEffectApplier, ParticipantEffectRefused,
)

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

    ``accounts_for(grantor_subject)`` is that grantor's connected-account store,
    composed with the shared account lock. STAGE holds the incarnation (a
    reconnect is refused meanwhile, and a stored account that is not that
    incarnation refuses the stage); COMMIT deletes exactly it and pins the
    outcome; ABORT releases the hold. A reconnection after the decision is
    another incarnation and is never deleted.
    """

    def __init__(self, accounts_for: Any) -> None:
        self._accounts_for = accounts_for

    def _accounts(self, payload: Mapping[str, Any]) -> Any:
        if self._accounts_for is None:
            raise ParticipantEffectRefused("card_effect_adapter_unavailable")
        accounts = self._accounts_for(payload["grantor_subject"])
        if accounts is None:
            raise ParticipantEffectRefused("card_effect_adapter_unavailable")
        return accounts

    async def _call(self, operation: Any) -> Any:
        from connection_hub.delegated_to_kdcube.store import AccountDisconnectPending, AccountLockUnavailable

        try:
            return await operation
        except AccountLockUnavailable:
            raise ParticipantEffectRefused("card_effect_adapter_unavailable") from None
        except AccountDisconnectPending:
            raise ParticipantEffectRefused("card_effect_binding_mismatch") from None

    async def prepare_once(self, binding: EffectBinding, payload: Mapping[str, Any]) -> str:
        accounts = self._accounts(payload)
        await self._call(accounts.hold_incarnation_for_delete(payload["account_id"], payload["incarnation"],
                                                              pin=binding.effect_digest))
        return binding.effect_digest

    async def apply_once(self, binding: EffectBinding, payload: Mapping[str, Any]) -> str:
        accounts = self._accounts(payload)
        # The effect's own identity pins the first completed outcome; a replay returns it.
        outcome = await self._call(accounts.disconnect_incarnation(payload["account_id"], payload["incarnation"],
                                                                   pin=binding.effect_digest))
        if outcome not in ("disconnected", "account_absent", "account_reconnected"):
            raise ParticipantEffectRefused("card_effect_binding_mismatch")
        return binding.effect_digest

    async def release_once(self, binding: EffectBinding, payload: Mapping[str, Any]) -> str:
        accounts = self._accounts(payload)
        await self._call(accounts.release_incarnation_hold(payload["account_id"], pin=binding.effect_digest))
        return binding.effect_digest


class CredentialIssueTarget:
    """W603: one original OAuth credential, reserved before its decision and activated by its COMMIT.

    The reservation is keyed by the DECISION's transaction id: a group
    member's receipt carries it as ``group.transaction_id``. The Card fence is
    read under the Card's own section (FINISH holds it while effects apply):
    while this transaction's pointer is still the Card's current pointer the
    Card is exactly its AFTER; once anything else is current, that is the
    authoritative Card. Either way it must be exactly the committed revision
    object (its content hash, so a replacement Card at any revision never
    matches), active and unexpired by the database clock, or the reservation
    is ``superseded``. Family rows are never the evidence: a newer Card with no
    family at all still supersedes.

    With ``credential_handles`` (the Card's handle-metadata store), the access
    slot's activation also writes the committed revision's handle metadata,
    exactly as the direct path's ``persist`` does after its commit, so the
    issued Card stays readable; a replay writes the same metadata again.
    """

    def __init__(self, issuance_store: Any, card_store: Any, credential_handles: Any = None) -> None:
        self._grant_store = issuance_store
        self._card_store = card_store
        self._credential_handles = credential_handles

    @staticmethod
    def _decision_id(binding: EffectBinding) -> str:
        group = binding.receipt().get("group")
        return group["transaction_id"] if isinstance(group, Mapping) else binding.transaction_id

    def _call(self, name: str) -> Any:
        operation = getattr(self._grant_store, name, None)
        if operation is None:
            raise ParticipantEffectRefused("card_effect_adapter_unavailable")
        return operation

    async def _live_card(self, binding: EffectBinding, payload: Mapping[str, Any]) -> Any:
        """The committed revision when it is still the authoritative live Card; otherwise None."""
        receipt = binding.receipt()
        subject_hash, access_id = receipt["subject_hash"], receipt["access_id"]
        raw = await read_json_or_none(self._card_store.current_path(subject_hash=subject_hash, access_id=access_id))
        if (isinstance(raw, Mapping) and raw.get("schema") == tx.TRANSACTION_POINTER_SCHEMA
                and raw.get("transaction_id") == binding.transaction_id):
            pointer = CardCurrentPointer.from_mapping(receipt["after"])
            authority = await self._card_store.read_revision(
                subject_hash=subject_hash, access_id=access_id, revision_name=pointer.revision_name)
        else:
            current = await self._card_store.read_current_authority(subject_hash=subject_hash, access_id=access_id)
            authority = None if current is None else current[1]
        if authority is None:
            return None
        committed = CardCurrentPointer.from_mapping(receipt["after"])
        now = await self._call("issuance_clock")()
        live = (authority.access_id == payload["access_id"] == committed.access_id
                and authority.card_revision == payload["card_revision"] == committed.card_revision
                and authority.content_hash() == committed.content_hash
                and authority.state == CARD_STATE_ACTIVE and authority.expires_at == payload["expires_at"]
                and authority.expires_at > now)
        return authority if live else None

    async def _store(self, operation: Any) -> str:
        from ..oauth.issuance_store import IssuanceStoreRefused

        try:
            return await operation
        except IssuanceStoreRefused as exc:
            raise ParticipantEffectRefused(
                "card_effect_target_revision_moved" if exc.reason == "reservation_window_closed"
                else "card_effect_binding_mismatch") from None

    async def prepare_once(self, binding: EffectBinding, payload: Mapping[str, Any]) -> str:
        outcome = await self._store(self._call("bind_issued_credential")(
            transaction_id=self._decision_id(binding), slot=payload["slot"], effect_digest=binding.effect_digest,
            access_id=payload["access_id"], card_revision=payload["card_revision"]))
        if outcome != "bound":
            raise ParticipantEffectRefused("card_effect_binding_mismatch")
        return binding.effect_digest

    async def apply_once(self, binding: EffectBinding, payload: Mapping[str, Any]) -> str:
        live = await self._live_card(binding, payload)
        outcome = await self._store(self._call("activate_issued_credential")(
            transaction_id=self._decision_id(binding), slot=payload["slot"], effect_digest=binding.effect_digest,
            card_live=live is not None))
        if outcome == "applied":
            # W606: only a CREATE writes the new Card's handle metadata here; every later Card change
            # moves an existing row through its own ``handle_binding`` effect (one writer per row).
            created = binding.receipt().get("before") is None
            if (created and payload["slot"] == "access" and self._credential_handles is not None
                    and live is not None):
                from .model import CardCredentialHandles
                try:
                    await self._credential_handles.write(live, CardCredentialHandles(access_id=live.access_id))
                except Exception:
                    raise ParticipantEffectRefused("card_effect_target_unavailable") from None
            return binding.effect_digest
        if outcome == CREDENTIAL_ISSUE_SUPERSEDED:
            return CREDENTIAL_ISSUE_SUPERSEDED
        raise ParticipantEffectRefused("card_effect_binding_mismatch")

    async def release_once(self, binding: EffectBinding, payload: Mapping[str, Any]) -> str:
        await self._store(self._call("release_issued_credential")(
            transaction_id=self._decision_id(binding), slot=payload["slot"]))
        return binding.effect_digest


class HandleBindingTarget:
    """W606: move one member Card's handle row to the committed AFTER; the credential itself never changes.

    The effect's payload pinned the row's whole identity when the edit's
    intent was built. STAGE refuses unless the active row is still exactly
    that identity (the edit then ABORTs and the Card keeps its readable
    revision). COMMIT applies the compare-and-set only while the member Card
    is still exactly its committed AFTER (content hash, active), the same
    fence as ``CredentialIssueTarget``; otherwise, or when the row moved, it
    pins ``superseded`` and writes nothing. A replay returns the same answer.
    """

    def __init__(self, credential_handles: Any, card_store: Any) -> None:
        self._handles = credential_handles
        self._card_store = card_store

    def _operation(self, name: str) -> Any:
        operation = getattr(self._handles, name, None)
        if operation is None:
            raise ParticipantEffectRefused("card_effect_adapter_unavailable")
        return operation

    async def _member_receipt(self, binding: EffectBinding, access_id: str) -> tuple[str, Mapping[str, Any]]:
        """(member transaction id, its receipt) of the Card this effect names, in a single Card or a group."""
        receipt = binding.receipt()
        group = receipt.get("group")
        if not isinstance(group, Mapping):
            return binding.transaction_id, receipt
        aggregate = await tx.read_receipt(self._card_store, group["transaction_id"])
        for member in (aggregate or {}).get("members") or ():
            if member.get("access_id") == access_id:
                found = await tx.read_receipt(self._card_store, member["transaction_id"])
                if found is not None:
                    return member["transaction_id"], found
        raise ParticipantEffectRefused("card_effect_binding_mismatch")

    async def _committed_is_live(self, binding: EffectBinding, payload: Mapping[str, Any]) -> bool:
        member_id, receipt = await self._member_receipt(binding, payload["access_id"])
        committed = CardCurrentPointer.from_mapping(receipt["after"])
        if committed.state != CARD_STATE_ACTIVE:
            return False  # an ending Card never re-points an active binding
        subject_hash, access_id = receipt["subject_hash"], receipt["access_id"]
        raw = await read_json_or_none(self._card_store.current_path(subject_hash=subject_hash, access_id=access_id))
        if (isinstance(raw, Mapping) and raw.get("schema") == tx.TRANSACTION_POINTER_SCHEMA
                and raw.get("transaction_id") == member_id):
            return True  # this transaction's own pointer: the Card is exactly its AFTER
        current = await self._card_store.read_current_authority(subject_hash=subject_hash, access_id=access_id)
        return (current is not None and current[1].content_hash() == committed.content_hash
                and current[1].card_revision == committed.card_revision and current[1].state == CARD_STATE_ACTIVE)

    async def prepare_once(self, binding: EffectBinding, payload: Mapping[str, Any]) -> str:
        identity = await self._operation("binding_identity")(payload["access_id"])
        if not isinstance(identity, Mapping) or (identity.get("from_identity"), identity.get("from_revision"),
                                                 identity.get("from_expires_at")) != (
                payload["from_identity"], payload["from_revision"], payload["from_expires_at"]):
            raise ParticipantEffectRefused("card_effect_target_revision_moved")
        return binding.effect_digest

    async def apply_once(self, binding: EffectBinding, payload: Mapping[str, Any]) -> str:
        if not await self._committed_is_live(binding, payload):
            return CREDENTIAL_ISSUE_SUPERSEDED
        if payload["from_fingerprint"]:
            # An agent row: re-wrap the same bearer under a fresh ref bound to the AFTER Card.
            outcome = await self._operation("rebind_resident")(
                payload["access_id"], from_identity=payload["from_identity"],
                from_fingerprint=payload["from_fingerprint"], from_revision=payload["from_revision"],
                from_expires_at=payload["from_expires_at"], to_revision=payload["card_revision"],
                to_expires_at=payload["expires_at"])
        else:
            outcome = await self._operation("advance_binding")(
                payload["access_id"], from_identity=payload["from_identity"], from_revision=payload["from_revision"],
                from_expires_at=payload["from_expires_at"], to_revision=payload["card_revision"],
                to_expires_at=payload["expires_at"])
        if outcome == "applied":
            return binding.effect_digest
        if outcome == CREDENTIAL_ISSUE_SUPERSEDED:
            return CREDENTIAL_ISSUE_SUPERSEDED
        raise ParticipantEffectRefused("card_effect_binding_mismatch")

    async def release_once(self, binding: EffectBinding, payload: Mapping[str, Any]) -> str:
        return binding.effect_digest  # STAGE held nothing: the row moves only at COMMIT


def compose_card_effects(*, card_service: Any, card_store: Any, grant_store: Any,
                         policies: Any, accounts_for: Any = None, issuance_store: Any = None,
                         credential_handles: Any = None) -> ParticipantEffectApplier:
    """Bind one validated applier to a Card service's effect hooks.

    The receipt reader is the Card store's own durable receipt; the targets are
    the ones above. ``grant_binding`` stays refused by the applier.
    ``issuance_store`` is the PostgreSQL OAuth authority holding W603's
    reservations; without it ``credential_issue`` refuses (adapter unavailable).
    """

    async def read_receipt(transaction_id: str) -> Mapping[str, Any] | None:
        return await tx.read_receipt(card_store, transaction_id)

    applier = ParticipantEffectApplier(read_receipt=read_receipt, targets={
        "credential_lifetime": CredentialLifetimeTarget(grant_store),
        "invocation_policy": InvocationPolicyTarget(policies),
        "grant_unbind": GrantUnbindTarget(grant_store),
        "account_delete": AccountDeleteTarget(accounts_for),
        "credential_issue": CredentialIssueTarget(issuance_store, card_store, credential_handles),
        "handle_binding": HandleBindingTarget(credential_handles, card_store),
    })
    card_service.bind_effect_applier(applier.apply)
    card_service.bind_effect_preparer(applier.prepare)
    card_service.bind_effect_releaser(applier.release)
    return applier


__all__ = ["AccountDeleteTarget", "CredentialIssueTarget", "HandleBindingTarget", "CredentialLifetimeTarget", "GrantUnbindTarget", "InvocationPolicyTarget",
           "NO_ACTIVE_CREDENTIALS", "compose_card_effects"]
