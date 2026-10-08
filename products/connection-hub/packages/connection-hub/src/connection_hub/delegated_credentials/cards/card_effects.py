"""W578: the versioned Hub input for a transaction that changes NO Card but applies a bounded effect.

A disconnect of a connected account no Card binds still deletes state with a
lifetime of its own (the account's exact incarnation, S2), so it takes the
same durable COMMIT/ABORT as every Card change (CodeApp, 7 October 2026 02:45
UTC: "the SAME generic durable COMMIT/ABORT coordinator with an account-delete
effect-only participant input"). There is no Card to stage, so this is a
fourth binding on the SAME participant ``connection-hub.card`` and the same
prepare, Decision and finish; an empty group stays invalid:

- ``binding_kind`` ``connection-hub.card-effects``; ``binding_ref``
  ``effects:<sha256(canonical candidate_value)>``; ``target_scope`` the sha256
  over the owner's one subject hash; ``target_incarnation`` 1; ``action``
  ``effect``; ``before_revision`` 1 and ``candidate_revision`` 1 (the Hub
  refuses any other pair); ``candidate_digest`` sha256 of the candidate value;
  ``dependency_revisions`` empty (the account fence and the incarnation hold
  are the effect's own state, held at STAGE); one actor; ``provisioning``
  ``{}``.
- The candidate value is ``{"schema": "connection-hub.card-effects.v1",
  "subject_hash": "<64 hex>", "effects": [...]}``: one owner, 1 to
  ``MAX_EFFECTS_ONLY`` effects sorted and unique by key. Version 1 admits
  ``account_delete`` only, bound to that owner, its provider and account and
  the incarnation read before the transaction began; its ``access_id`` is
  ``""`` because no Card carries it.

A peer's transaction authority can never send this input: account deletion is
initiated by the Hub only, and the authority's single-Card candidate check
refuses any other shape.
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping, Sequence

from service_foundation.coordination.durable_decision_log import DecisionRefused
from service_foundation.coordination.durable_wire import WireRefused, canonical_json_bytes, sha256_hex

from .card_participant import PARTICIPANT

EFFECTS_BINDING_KIND = "connection-hub.card-effects"
EFFECTS_SCHEMA = "connection-hub.card-effects.v1"
EFFECTS_ONLY_KINDS = frozenset({"account_delete"})
MAX_EFFECTS_ONLY = 4
_ACCOUNT_DELETE_FIELDS = frozenset({"access_id", "grantor_subject", "provider_id", "account_id", "incarnation"})
_HEX = frozenset("0123456789abcdef")


def _refuse(reason: str) -> DecisionRefused:
    return DecisionRefused(reason)


def _exact_int(value: Any, expected: int = 1) -> bool:
    return type(value) is int and value == expected


def _is_hex64(value: Any) -> bool:
    return type(value) is str and len(value) == 64 and set(value) <= _HEX


def _bounded_text(value: Any) -> bool:
    return type(value) is str and 0 < len(value) <= 256 and value == value.strip()


def effects_candidate_value(subject_hash: str, effects: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {"schema": EFFECTS_SCHEMA, "subject_hash": subject_hash,
            "effects": sorted((dict(effect) for effect in effects), key=lambda effect: str(effect.get("key")))}


def validate_effects_candidate(value: Any) -> dict[str, Any]:
    """The canonical effects-only candidate, or a named refusal."""
    if (not isinstance(value, Mapping) or set(value) != {"schema", "subject_hash", "effects"}
            or value["schema"] != EFFECTS_SCHEMA or not _is_hex64(value["subject_hash"])
            or type(value["effects"]) is not list):
        raise _refuse("card_effects_invalid")
    effects = value["effects"]
    if not effects:
        raise _refuse("card_effects_empty")
    if len(effects) > MAX_EFFECTS_ONLY:
        raise _refuse("card_effects_too_large")
    keys = []
    for effect in effects:
        if (not isinstance(effect, Mapping) or set(effect) != {"kind", "key", "payload"}
                or effect["kind"] not in EFFECTS_ONLY_KINDS or type(effect["key"]) is not str
                or not isinstance(effect["payload"], Mapping)):
            raise _refuse("card_effects_effect_invalid")
        payload = effect["payload"]
        if (set(payload) != _ACCOUNT_DELETE_FIELDS or payload["access_id"] != ""
                or not all(_bounded_text(payload[name])
                           for name in ("grantor_subject", "provider_id", "account_id", "incarnation"))
                or effect["key"] != f"{payload['provider_id']}:{payload['account_id']}"):
            raise _refuse("card_effects_effect_invalid")
        if hashlib.sha256(payload["grantor_subject"].encode("utf-8")).hexdigest() != value["subject_hash"]:
            raise _refuse("card_effects_owner_mismatch")
        keys.append(effect["key"])
    if len(set(keys)) != len(keys):
        raise _refuse("card_effects_effect_duplicate")
    if keys != sorted(keys):
        raise _refuse("card_effects_not_canonical")
    try:
        canonical_json_bytes(dict(value))
    except WireRefused as exc:
        raise _refuse("card_effects_invalid") from exc
    return dict(value)


def _aggregate(value: Mapping[str, Any]) -> dict[str, Any]:
    return {"binding_ref": "effects:" + sha256_hex(canonical_json_bytes(dict(value))),
            "target_scope": sha256_hex(canonical_json_bytes({"scopes": [value["subject_hash"]]})),
            "candidate_digest": sha256_hex(canonical_json_bytes(dict(value)))}


def hub_effects_participant_input(*, subject_hash: str, effects: Sequence[Mapping[str, Any]], actor_subject: str,
                                  actor_kind: str) -> dict[str, Any]:
    """The Hub's effects-only ``participant_inputs[PARTICIPANT]``; refuses one the Hub would never prepare."""
    if (actor_kind not in ("caller", "grantor") or type(actor_subject) is not str or not actor_subject.strip()
            or actor_subject != actor_subject.strip()):
        raise _refuse("card_effects_actor_invalid")
    value = validate_effects_candidate(effects_candidate_value(subject_hash, effects))
    return {"participant": PARTICIPANT, "binding_kind": EFFECTS_BINDING_KIND, **_aggregate(value),
            "target_incarnation": 1, "action": "effect", "before_revision": 1, "candidate_revision": 1,
            "dependency_revisions": {}, "actor_subject": actor_subject, "actor_kind": actor_kind,
            "provisioning": {}}


def verify_effects_projection(projection: Mapping[str, Any], value: Any) -> dict[str, Any]:
    """The effects-only candidate a verified projection names, or a named refusal (every field compared)."""
    if projection.get("binding_kind") != EFFECTS_BINDING_KIND:
        raise _refuse("card_effects_binding_invalid")
    checked = validate_effects_candidate(value)
    expected = _aggregate(checked)
    if (projection.get("participant") != PARTICIPANT
            or any(projection.get(name) != expected[name] for name in expected)
            or not _exact_int(projection.get("target_incarnation")) or projection.get("action") != "effect"
            or not _exact_int(projection.get("before_revision"))
            or not _exact_int(projection.get("candidate_revision"))
            or projection.get("dependency_revisions") != {} or projection.get("provisioning") != {}
            or projection.get("actor_kind") not in ("caller", "grantor")
            or type(projection.get("actor_subject")) is not str or not projection["actor_subject"].strip()
            or projection["actor_subject"] != projection["actor_subject"].strip()):
        raise _refuse("card_effects_not_bound")
    return checked


def effect_accounts(effects: Sequence[Mapping[str, Any]]) -> list[tuple[str, str]]:
    """The (provider, account) every account_delete effect deletes, sorted: the accounts the fence must hold."""
    return sorted({(str(effect["payload"]["provider_id"]), str(effect["payload"]["account_id"]))
                   for effect in effects if effect.get("kind") == "account_delete"})


__all__ = ["EFFECTS_BINDING_KIND", "EFFECTS_ONLY_KINDS", "EFFECTS_SCHEMA", "MAX_EFFECTS_ONLY",
           "effect_accounts", "effects_candidate_value", "hub_effects_participant_input",
           "validate_effects_candidate", "verify_effects_projection"]
