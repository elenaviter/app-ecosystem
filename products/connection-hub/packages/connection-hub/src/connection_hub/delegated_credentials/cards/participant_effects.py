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
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Protocol

from .model import CardCurrentPointer

EFFECT_KINDS = frozenset({
    "grant_binding", "credential_lifetime", "invocation_policy", "grant_unbind",
})
_HEX = re.compile(r"[0-9a-f]{64}")
_SECRET_KEYS = frozenset({
    "token", "access_token", "refresh_token", "bearer", "password", "secret",
    "client_secret", "credential", "credentials", "authorization", "cookie",
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
            if key.lower().replace("-", "_") in _SECRET_KEYS:
                _refuse("card_effect_secret_field")
            _no_secret_fields(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _no_secret_fields(item, depth + 1)


def _payload(kind: str, key: str, value: Any, access_id: str) -> str:
    if not isinstance(value, Mapping):
        _refuse("card_effect_payload_invalid")
    _no_secret_fields(value)
    keys = {
        "grant_binding": {"access_id", "operations", "resource_grants",
                          "resource_operations", "named_services", "expires_at", "slot"},
        "credential_lifetime": {"access_id", "expires_at"},
        "invocation_policy": {"access_id", "mode"},
        "grant_unbind": {"access_id"},
    }[kind]
    if set(value) != keys or value.get("access_id") != access_id:
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
    elif kind == "credential_lifetime" and key not in {"access", "refresh", "card"}:
        _refuse("card_effect_payload_invalid")
    elif kind == "invocation_policy" and (
            type(value["mode"]) is not str or value["mode"] not in {"always", "once"}):
        _refuse("card_effect_payload_invalid")
    return _canonical(dict(value))


@dataclass(frozen=True)
class EffectBinding:
    """Immutable exact-replay identity; receipt JSON includes both Card vectors.

    The trusted receipt/intent binds the authorized actor. This helper neither
    infers a PB actor nor derives new authority from these stored coordinates.
    Target adapters must retain the identity without retaining raw secrets.
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
    after application or a proven identical prior application. A target or
    digest conflict raises. Resolve secrets by pinned opaque custody references,
    never from receipt values or mutable current handles on a late retry.
    """

    async def apply_once(self, binding: EffectBinding, payload: Mapping[str, Any]) -> str: ...


ReceiptReader = Callable[[str], Awaitable[Mapping[str, Any] | None]]


class ParticipantEffectApplier:
    def __init__(self, *, read_receipt: ReceiptReader,
                 targets: Mapping[str, IdempotentEffectTarget]) -> None:
        self._read_receipt = read_receipt
        self._targets = dict(targets)  # instance-local composition, not an authority cache

    async def __call__(self, kind: str, key: str, payload: Mapping[str, Any], *,
                       transaction_id: str) -> None:
        await self.apply(kind, key, payload, transaction_id=transaction_id)

    async def apply(self, kind: str, key: str, payload: Mapping[str, Any], *,
                    transaction_id: str) -> None:
        if type(transaction_id) is not str or not _HEX.fullmatch(transaction_id):
            _refuse("card_effect_transaction_invalid")
        if type(kind) is not str or kind not in EFFECT_KINDS:
            _refuse("card_effect_kind_invalid")
        if type(key) is not str or not key or len(key) > 2048:
            _refuse("card_effect_key_invalid")
        try:
            receipt = await self._read_receipt(transaction_id)
        except ParticipantEffectRefused:
            raise
        except Exception:
            raise ParticipantEffectRefused("card_effect_receipt_unavailable") from None
        if not isinstance(receipt, Mapping) or receipt.get("transaction_id") != transaction_id:
            _refuse("card_effect_receipt_binding_invalid")
        if receipt.get("state") != "committed":
            _refuse("card_effect_not_committed")
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
        requested = _payload(kind, key, payload, before.access_id)
        for effect in effects:
            if not isinstance(effect, Mapping) or set(effect) != {"kind", "key", "payload"}:
                _refuse("card_effect_set_invalid")
            effect_kind, effect_key = effect["kind"], effect["key"]
            if (type(effect_kind) is not str or effect_kind not in EFFECT_KINDS
                    or type(effect_key) is not str or not effect_key or len(effect_key) > 2048
                    or (effect_kind, effect_key) in identities):
                _refuse("card_effect_set_invalid")
            identities.add((effect_kind, effect_key))
            saved = _payload(effect_kind, effect_key, effect["payload"], before.access_id)
            if (effect_kind, effect_key) == (kind, key):
                if saved != requested:
                    _refuse("card_effect_digest_mismatch")
                matched = True
        if not matched:
            _refuse("card_effect_not_prepared")
        _no_secret_fields(receipt)
        receipt_json = _canonical(dict(receipt))
        effect_json = _canonical({"kind": kind, "key": key, "payload": json.loads(requested)})
        binding = EffectBinding(transaction_id, kind, key, _digest(effect_json),
                                _digest(receipt_json), receipt_json)
        target = self._targets.get(kind)
        if target is None:
            _refuse("card_effect_adapter_unavailable")
        try:
            applied_digest = await target.apply_once(binding, json.loads(requested))
        except ParticipantEffectRefused:
            raise
        except Exception:
            raise ParticipantEffectRefused("card_effect_target_unavailable") from None
        if applied_digest != binding.effect_digest:
            _refuse("card_effect_applied_receipt_invalid")


__all__ = ["EFFECT_KINDS", "EffectBinding", "IdempotentEffectTarget",
           "ParticipantEffectApplier", "ParticipantEffectRefused"]
