"""Strict, product-neutral bytes for a durable transaction authority.

The global intent is persisted as these canonical bytes. A participant's
projection is derived from the selected input in that persisted intent, never
from a later caller-supplied projection alone.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping


INTENT_SCHEMA = "durable-transaction-intent.v1"
PROJECTION_FIELDS = frozenset({
    "participant", "binding_kind", "binding_ref", "target_scope",
    "target_incarnation", "action", "before_revision", "candidate_revision",
    "candidate_digest", "dependency_revisions", "actor_subject", "actor_kind",
    "provisioning",
})


class WireRefused(ValueError):
    """An authority value cannot have the exact agreed wire representation."""


def _reject_float(_text: str) -> None:
    raise WireRefused("non_integer_number")


def _reject_constant(_text: str) -> None:
    raise WireRefused("non_json_number")


def _pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in values:
        if key in result:
            raise WireRefused("duplicate_json_key")
        result[key] = value
    return result


def _check(value: Any) -> None:
    if value is None or type(value) in (str, bool, int):
        if type(value) is str:
            try:
                value.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise WireRefused("invalid_unicode") from exc
        return
    if type(value) in (list, tuple):
        for element in value:
            _check(element)
        return
    if type(value) is dict:
        for key, element in value.items():
            if type(key) is not str:
                raise WireRefused("non_string_key")
            _check(key)
            _check(element)
        return
    raise WireRefused("unsupported_json_value")


def canonical_json_bytes(value: Any) -> bytes:
    """UTF-8 JSON: sorted keys, array order kept, integers only, no surrogates."""

    _check(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8", "strict")


def parse_canonical_json_bytes(data: bytes) -> Any:
    """Reject duplicate keys and any alternate encoding of the same value."""

    if type(data) is not bytes:
        raise WireRefused("wire_bytes_required")
    try:
        value = json.loads(data.decode("utf-8", "strict"), object_pairs_hook=_pairs,
                           parse_float=_reject_float, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WireRefused("invalid_json") from exc
    if canonical_json_bytes(value) != data:
        raise WireRefused("non_canonical_json")
    return value


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _nonempty(value: Any) -> bool:
    return type(value) is str and bool(value)


@dataclass(frozen=True)
class IntentDraft:
    """Application-owned request frozen before a store may await I/O.

    ``replay_scope`` is an opaque database uniqueness key, not a wire field.
    It can distinguish two actors using the same request ID in one namespace.
    """

    replay_scope: str
    request_id: str
    expires_at: int
    participants: tuple[str, ...]
    payload: dict[str, Any]
    _payload_bytes: bytes = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (not _nonempty(self.replay_scope) or not _nonempty(self.request_id)
                or type(self.expires_at) is not int or self.expires_at < 0
                or type(self.participants) not in (tuple, list)
                or not self.participants
                or any(not _nonempty(name) for name in self.participants)
                or len(set(self.participants)) != len(self.participants)
                or type(self.payload) is not dict):
            raise WireRefused("intent_draft_invalid")
        object.__setattr__(self, "participants", tuple(self.participants))
        object.__setattr__(self, "_payload_bytes", canonical_json_bytes(self.payload))

    def bind(self, transaction_id: str, epoch: int) -> GlobalIntent:
        return GlobalIntent(transaction_id, epoch, self.request_id, self.expires_at,
                            self.participants,
                            parse_canonical_json_bytes(self._payload_bytes))


@dataclass(frozen=True)
class GlobalIntent:
    """The exact seven-key v1 global intent; coordinates are bound before hash."""

    transaction_id: str
    epoch: int
    request_id: str
    expires_at: int
    participants: tuple[str, ...]
    payload: dict[str, Any]
    _bytes: bytes = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (not _nonempty(self.transaction_id) or type(self.epoch) is not int
                or self.epoch < 1 or not _nonempty(self.request_id)
                or type(self.expires_at) is not int or self.expires_at < 0
                or type(self.participants) not in (tuple, list)
                or not self.participants
                or any(not _nonempty(name) for name in self.participants)
                or len(set(self.participants)) != len(self.participants)
                or type(self.payload) is not dict):
            raise WireRefused("intent_invalid")
        object.__setattr__(self, "participants", tuple(self.participants))
        object.__setattr__(self, "_bytes", canonical_json_bytes({
            "schema": INTENT_SCHEMA,
            "transaction_id": self.transaction_id,
            "epoch": self.epoch,
            "request_id": self.request_id,
            "expires_at": self.expires_at,
            "participants": list(self.participants),
            "payload": self.payload,
        }))

    @property
    def canonical_bytes(self) -> bytes:
        return self._bytes

    @property
    def digest(self) -> str:
        return sha256_hex(self._bytes)

    def as_mapping(self) -> dict[str, Any]:
        return parse_canonical_json_bytes(self._bytes)

    @classmethod
    def from_canonical_bytes(cls, data: bytes) -> GlobalIntent:
        value = parse_canonical_json_bytes(data)
        if type(value) is not dict or set(value) != {
            "schema", "transaction_id", "epoch", "request_id", "expires_at",
            "participants", "payload",
        } or value["schema"] != INTENT_SCHEMA:
            raise WireRefused("intent_schema_invalid")
        if type(value["participants"]) is not list:
            raise WireRefused("intent_invalid")
        return cls(value["transaction_id"], value["epoch"], value["request_id"],
                   value["expires_at"], tuple(value["participants"]), value["payload"])


def participant_projection(intent: GlobalIntent, participant: str) -> dict[str, Any]:
    """Select the exact persisted input and bind its global digest."""

    if participant not in intent.participants:
        raise WireRefused("participant_unknown")
    inputs = intent.as_mapping()["payload"].get("participant_inputs")
    if type(inputs) is not dict or set(inputs) != set(intent.participants):
        raise WireRefused("participant_inputs_invalid")
    selected = inputs[participant]
    if type(selected) is not dict or set(selected) != PROJECTION_FIELDS:
        raise WireRefused("participant_input_invalid")
    if selected["participant"] != participant:
        raise WireRefused("participant_input_mismatch")
    before = selected["before_revision"]
    candidate = selected["candidate_revision"]
    if (type(before) is not int or before < 0
            or before == 0 and selected["action"] != "create"
            or type(candidate) is not int or candidate < 1):
        raise WireRefused("revision_invalid")
    dependencies = selected["dependency_revisions"]
    if (type(dependencies) is not dict
            or any(type(name) is not str or not name
                   or type(revision) is not int or revision < 1
                   for name, revision in dependencies.items())):
        raise WireRefused("dependency_revision_invalid")
    return {"global_intent_digest": intent.digest, **selected}


def projection_digest(intent: GlobalIntent, participant: str) -> str:
    return sha256_hex(canonical_json_bytes(participant_projection(intent, participant)))


def verify_participant_projection(intent: GlobalIntent, participant: str,
                                  supplied: Mapping[str, Any]) -> str:
    """Refuse a self-consistent projection that differs from persisted input."""

    if type(supplied) is not dict:
        raise WireRefused("projection_invalid")
    expected = participant_projection(intent, participant)
    if canonical_json_bytes(supplied) != canonical_json_bytes(expected):
        raise WireRefused("projection_mismatch")
    return sha256_hex(canonical_json_bytes(expected))


__all__ = ["GlobalIntent", "IntentDraft", "INTENT_SCHEMA", "PROJECTION_FIELDS", "WireRefused",
           "canonical_json_bytes", "parse_canonical_json_bytes", "participant_projection",
           "projection_digest", "sha256_hex", "verify_participant_projection"]
