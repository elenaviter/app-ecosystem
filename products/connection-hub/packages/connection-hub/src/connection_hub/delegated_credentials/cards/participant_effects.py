# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Validate and dispatch only the effects of an exact committed Card receipt.

This is a participant-local adapter, not a decision log or another writer.
The hosting composition injects its trusted, validated receipt reader and
source-bound target adapters. The existing transaction-store FINISH owns the
applied markers and readiness fence; this module never retires that fence.

An adapter's ``apply_once`` must enforce the complete EffectBinding atomically
with its target write, or use an intrinsically idempotent bound operation. It
must return the same effect digest after a successful exact replay, including
after a crash before FINISH writes its marker. A generic mint followed by a
handle write, a relative-TTL extension, or an unconditional policy set does
not implement this contract. Raw credentials remain inside host custody.

Expiry is absolute and is forwarded unchanged, even when already in the past.
The target adapter must apply expiry without reviving an expired credential.
An unavailable or unqualified adapter refuses; there is no legacy fallback.
Grant minting is source-gated until the SDK's bound no-second-mint custody
operation qualifies; supplying a target object does not enable it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Protocol

from .model import CardCurrentPointer
from connection_hub.invocation_policy.models import InvocationAuthority, validated_invocation_id

EFFECT_KINDS = frozenset({
    "grant_binding", "credential_lifetime", "invocation_policy", "grant_unbind",
})
_HEX = re.compile(r"[0-9a-f]{64}")
_SECRET_KEYS = frozenset({
    "token", "access_token", "refresh_token", "bearer", "password", "secret",
    "client_secret", "credential", "credentials", "authorization", "cookie",
    "id_token", "api_key", "session_token", "private_key", "refresh", "x_api_token",
})
_SECRET_FIELD = re.compile(r"(?:[a-z0-9]+_)*(?:token|secret|password|bearer|credential|credentials|api_key|private_key)(?:_[a-z0-9]+)*")
_SAFE_TARGET_REASONS = frozenset({
    "card_effect_adapter_unavailable", "card_effect_binding_mismatch",
    "card_effect_target_revision_moved", "card_effect_custody_unavailable",
    "card_effect_policy_conflict", "card_effect_old_handle_invalid",
})


class ParticipantEffectRefused(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _refuse(reason: str) -> None:
    raise ParticipantEffectRefused(reason)


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        _refuse("card_effect_record_invalid")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _no_secret_fields(value: Any, depth: int = 0) -> None:
    if depth > 16:
        _refuse("card_effect_record_invalid")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if type(key) is not str:
                _refuse("card_effect_record_invalid")
            normalized = key.lower().replace("-", "_")
            if normalized == "token_sha256":
                if type(item) is not str or not _HEX.fullmatch(item):
                    _refuse("card_effect_old_handle_invalid")
                continue  # exact digest metadata, never a raw credential
            if normalized in _SECRET_KEYS or _SECRET_FIELD.fullmatch(normalized):
                _refuse("card_effect_secret_field")
            _no_secret_fields(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _no_secret_fields(item, depth + 1)


def _payload(kind: str, key: str, value: Any, access_id: str,
             base_revision: int, subject_hash: str) -> str:
    if not isinstance(value, Mapping):
        _refuse("card_effect_payload_invalid")
    _no_secret_fields(value)
    keys = {
        "grant_binding": {"access_id", "operations", "resource_grants",
                          "resource_operations", "named_services", "expires_at", "slot"},
        "credential_lifetime": {"access_id", "expires_at", "base_card_revision"},
        "invocation_policy": {"owner_subject", "authority", "mode", "expected_revision"},
        "grant_unbind": {"access_id", "session_id", "token_sha256"},
    }[kind]
    if set(value) != keys or (kind != "invocation_policy" and value.get("access_id") != access_id):
        _refuse("card_effect_payload_binding_invalid")
    if "expires_at" in value and (
            type(value["expires_at"]) is not int or value["expires_at"] < 0):
        _refuse("card_effect_deadline_invalid")
    if kind == "grant_binding":
        if value["slot"] != key or not isinstance(value["operations"], list):
            _refuse("card_effect_payload_invalid")
        if any(type(operation) is not str or not operation for operation in value["operations"]):
            _refuse("card_effect_payload_invalid")
        if any(not isinstance(value[field], Mapping) for field in (
                "resource_grants", "resource_operations", "named_services")):
            _refuse("card_effect_payload_invalid")
    elif kind == "credential_lifetime":
        if key not in {"access", "refresh", "card"}:
            _refuse("card_effect_payload_invalid")
        if type(value["base_card_revision"]) is not int or value["base_card_revision"] != base_revision:
            _refuse("card_effect_base_revision_mismatch")
    elif kind == "invocation_policy":
        owner = value["owner_subject"]
        if (type(owner) is not str or not owner or owner != owner.strip()
                or hashlib.sha256(owner.encode("utf-8")).hexdigest() != subject_hash):
            _refuse("card_effect_owner_mismatch")
        if (type(value["mode"]) is not str or value["mode"] not in {"always", "once"}
                or type(value["expected_revision"]) is not int or value["expected_revision"] < 0):
            _refuse("card_effect_payload_invalid")
        try:
            # The backing policy port forms tx:key; never shorten that identity.
            validated_invocation_id("0" * 64 + ":" + key)
        except ValueError:
            raise ParticipantEffectRefused("card_effect_key_invalid") from None
        try:
            authority = InvocationAuthority.from_mapping(value["authority"])
            if (authority.to_dict() != value["authority"] or authority.access_id != access_id
                    or authority.key != key):
                _refuse("card_effect_policy_binding_invalid")
        except (ValueError, TypeError, AttributeError):
            raise ParticipantEffectRefused("card_effect_policy_binding_invalid") from None
    elif kind == "grant_unbind":
        if (type(value["session_id"]) is not str or not value["session_id"]
                or len(value["session_id"]) > 256 or type(value["token_sha256"]) is not str
                or not _HEX.fullmatch(value["token_sha256"])):
            _refuse("card_effect_old_handle_invalid")
    return _canonical(dict(value))


@dataclass(frozen=True)
class EffectBinding:
    """Immutable exact-replay identity; receipt JSON includes both Card vectors.

    The trusted receipt/intent binds the authorized actor. This helper neither
    infers a PB actor nor derives new authority from these stored coordinates.
    Target adapters must retain the identity without retaining raw secrets.
    Mutable decision state/reason are excluded, so all three phases share it.
    """

    transaction_id: str
    kind: str
    key: str
    effect_digest: str
    receipt_digest: str
    receipt_json: str

    def receipt(self) -> dict[str, Any]:
        return json.loads(self.receipt_json)


class IdempotentEffectTarget(Protocol):
    """The host's qualified, durable, revision-bound target operation.

    No ``None``/truthy success conversion: return the exact effect digest only
    after application or a proven identical prior application. Lifetime apply
    may instead return ``no_active_credentials``, a durable named no-op that
    must stay identical on replay. No other named outcome is accepted. A target or
    digest conflict raises. Resolve secrets by pinned opaque custody references,
    never from receipt values or mutable current handles on a late retry.
    """

    async def apply_once(self, binding: EffectBinding, payload: Mapping[str, Any]) -> str: ...

    async def prepare_once(self, binding: EffectBinding, payload: Mapping[str, Any]) -> str: ...

    async def release_once(self, binding: EffectBinding, payload: Mapping[str, Any]) -> str: ...


ReceiptReader = Callable[[str], Awaitable[Mapping[str, Any] | None]]


class ParticipantEffectApplier:
    def __init__(self, *, read_receipt: ReceiptReader,
                 targets: Mapping[str, IdempotentEffectTarget]) -> None:
        self._read_receipt = read_receipt
        self._targets = dict(targets)  # instance-local composition, not an authority cache

    async def __call__(self, kind: str, key: str, payload: Mapping[str, Any], *,
                       transaction_id: str) -> str:
        return await self.apply(kind, key, payload, transaction_id=transaction_id)

    async def apply(self, kind: str, key: str, payload: Mapping[str, Any], *,
                    transaction_id: str) -> str:
        """Return the validated result for the core's durable per-effect outcome."""
        return await self._dispatch("apply", kind, key, payload, transaction_id=transaction_id)

    async def prepare(self, kind: str, key: str, payload: Mapping[str, Any], *,
                      transaction_id: str) -> str:
        """STAGE only: place the invocation-policy marker, without granting it."""
        return await self._dispatch("prepare", kind, key, payload, transaction_id=transaction_id)

    async def release(self, kind: str, key: str, payload: Mapping[str, Any], *,
                      transaction_id: str) -> str:
        """Recorded ABORT only: release the exact policy preparation on recovery."""
        return await self._dispatch("release", kind, key, payload, transaction_id=transaction_id)

    async def _dispatch(self, phase: str, kind: str, key: str, payload: Mapping[str, Any], *,
                        transaction_id: str) -> str:
        if type(transaction_id) is not str or not _HEX.fullmatch(transaction_id):
            _refuse("card_effect_transaction_invalid")
        if type(kind) is not str or kind not in EFFECT_KINDS:
            _refuse("card_effect_kind_invalid")
        if type(key) is not str or not key or len(key) > 2048:
            _refuse("card_effect_key_invalid")
        try:
            receipt = await self._read_receipt(transaction_id)
        except Exception:
            raise ParticipantEffectRefused("card_effect_receipt_unavailable") from None
        if not isinstance(receipt, Mapping) or receipt.get("transaction_id") != transaction_id:
            _refuse("card_effect_receipt_binding_invalid")
        state = {"apply": "committed", "prepare": "prepared", "release": "aborted"}[phase]
        if receipt.get("state") != state:
            _refuse("card_effect_not_" + state)
        try:
            if any(type(receipt[field].get(name)) is not int for field in ("before", "after")
                   for name in ("card_revision", "expires_at")):
                _refuse("card_effect_receipt_binding_invalid")
            before, after = (CardCurrentPointer.from_mapping(receipt[field]) for field in ("before", "after"))
            if (before.to_dict() != receipt["before"] or after.to_dict() != receipt["after"]
                    or not before.access_id or before.access_id != receipt["access_id"]
                    or after.access_id != before.access_id or before.card_revision < 1
                    or after.card_revision != before.card_revision + 1
                    or not _HEX.fullmatch(receipt["subject_hash"])
                    or not _HEX.fullmatch(receipt["intent_digest"])
                    or not _HEX.fullmatch(receipt["change_digest"])
                    or not _HEX.fullmatch(before.content_hash)
                    or not _HEX.fullmatch(after.content_hash)
                    or type(receipt["participant"]) is not str or not receipt["participant"]):
                _refuse("card_effect_receipt_binding_invalid")
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ParticipantEffectRefused("card_effect_receipt_binding_invalid") from None
        effects = receipt.get("effects")
        if not isinstance(effects, list) or not 1 <= len(effects) <= 32:
            _refuse("card_effect_set_invalid")
        identities = set()
        matched = False
        requested = _payload(kind, key, payload, before.access_id, before.card_revision, receipt["subject_hash"])
        for effect in effects:
            if not isinstance(effect, Mapping) or set(effect) != {"kind", "key", "payload"}:
                _refuse("card_effect_set_invalid")
            effect_kind, effect_key = effect["kind"], effect["key"]
            if (type(effect_kind) is not str or effect_kind not in EFFECT_KINDS
                    or type(effect_key) is not str or not effect_key or len(effect_key) > 2048
                    or (effect_kind, effect_key) in identities):
                _refuse("card_effect_set_invalid")
            identities.add((effect_kind, effect_key))
            saved = _payload(effect_kind, effect_key, effect["payload"], before.access_id,
                             before.card_revision, receipt["subject_hash"])
            if (effect_kind, effect_key) == (kind, key):
                if saved != requested:
                    _refuse("card_effect_digest_mismatch")
                matched = True
        if not matched:
            _refuse("card_effect_not_prepared")
        _no_secret_fields(receipt)
        receipt_json = _canonical({field: value for field, value in receipt.items()
                                   if field not in {"state", "reason"}})
        effect_json = _canonical({"kind": kind, "key": key, "payload": json.loads(requested)})
        binding = EffectBinding(transaction_id, kind, key, _digest(effect_json),
                                _digest(receipt_json), receipt_json)
        if phase != "apply" and kind != "invocation_policy":
            return binding.effect_digest  # validated no-op; no target state is prepared/released
        if kind == "grant_binding":
            # A merely bound callback is not proof of crash-safe SDK mint/custody.
            _refuse("card_effect_mint_unqualified")
        target = self._targets.get(kind)
        if target is None:
            _refuse("card_effect_adapter_unavailable")
        try:
            operation = getattr(target, phase + "_once", None)
            if not callable(operation):
                _refuse("card_effect_adapter_unavailable")
            applied_digest = await operation(binding, json.loads(requested))
        except ParticipantEffectRefused as exc:
            # An adapter must not put raw secrets in a reason, even by accident.
            reason = exc.reason if type(exc.reason) is str and exc.reason in _SAFE_TARGET_REASONS else "card_effect_target_unavailable"
            raise ParticipantEffectRefused(reason) from None
        except Exception:
            raise ParticipantEffectRefused("card_effect_target_unavailable") from None
        if (phase == "apply" and kind == "credential_lifetime"
                and type(applied_digest) is str and applied_digest == "no_active_credentials"):
            return applied_digest
        if type(applied_digest) is not str or applied_digest != binding.effect_digest:
            _refuse("card_effect_applied_receipt_invalid")
        return applied_digest


__all__ = ["EFFECT_KINDS", "EffectBinding", "IdempotentEffectTarget",
           "ParticipantEffectApplier", "ParticipantEffectRefused"]
