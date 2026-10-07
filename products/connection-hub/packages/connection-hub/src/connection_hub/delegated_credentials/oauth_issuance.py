# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W603: the types and identities of an original OAuth issuance under ONE Card decision.

An authorization-code exchange grants a Card (or changes one) and issues that
Card's first credentials. Both belong to one Card decision on the existing
transaction protocol: ``begin`` plans the candidate Card and its
``credential_issue`` effects and begins the decision; the SDK mints and
reserves each credential against that plan; ``complete`` prepares, commits and
finishes the decision, and the COMMIT's effects activate the reserved
credentials. Nothing here is a second protocol or a new authority; this module
holds only the immutable shapes the two sides exchange.

``OAuthIssuancePlan`` is the PLANNED context, returned before COMMIT; it is
never a committed receipt. ``OAuthIssuanceResult`` is what the decision did:
it names the same transaction and intent digest, each slot's effect digest
(fixed in the plan) and, once committed, the Hub's committed receipt digest.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

ISSUANCE_PLAN_SCHEMA = "connection-hub.oauth-issuance-plan.v1"
ISSUANCE_SLOTS = ("access", "refresh")

# The bounded window in which the ORIGINAL response to an already validated and
# consumed authorization-code exchange may be recovered: the same client, PKCE
# verifier, redirect URI and input digest get the same pinned outcome and the
# same bearer, never a new mint. It is NOT the code's validity: the SDK's
# authorization code lives AUTH_CODE_TTL_SECONDS (60 s) and is validated live
# on the first exchange. A lost response is retried within seconds; 600 s
# bounds that recovery and is capped by the Card's own expiry. An agreed
# engineering choice (App, Infra, Main, 7 October 2026), not a measured value.
ISSUANCE_DELIVERY_SECONDS = 600


class IssuanceRefused(ValueError):
    """A named, bounded refusal; never carries a bearer, a code or a record value."""

    def __init__(self, reason: str, *, retryable: bool = False) -> None:
        self.reason = reason
        self.retryable = retryable
        super().__init__(reason)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def decision_request_id(*, scope: str, grantor_subject: str, client_id: str, original_request_id: str) -> str:
    """The decision's request id: a hash of a canonical, field-tagged mapping (no concatenation)."""
    return _sha256(_canonical({"scope": scope, "grantor_subject": grantor_subject, "client_id": client_id,
                               "original_request_id": original_request_id}))


def original_input_digest(inputs: Mapping[str, Any]) -> str:
    """The Hub's digest over every candidate-shaping input ``begin`` received (canonical JSON)."""
    return _sha256(_canonical({"schema": ISSUANCE_PLAN_SCHEMA, "inputs": dict(inputs)}))


def credential_issue_effect(*, access_id: str, slot: str, expires_at: int, card_revision: int) -> dict[str, Any]:
    return {"kind": "credential_issue", "key": slot,
            "payload": {"access_id": access_id, "slot": slot, "expires_at": expires_at,
                        "card_revision": card_revision}}


def effect_digest(effect: Mapping[str, Any]) -> str:
    """The digest the effect applier gives this effect's ``EffectBinding`` (participant_effects)."""
    return _sha256(_canonical({"kind": effect["kind"], "key": effect["key"], "payload": dict(effect["payload"])}))


@dataclass(frozen=True)
class OAuthIssuancePlan:
    """The planned, immutable context of one original issuance (pre-COMMIT)."""

    transaction_id: str
    decision_request_id: str
    intent_digest: str
    original_input_digest: str
    tenant: str
    project: str
    access_id: str
    grantor_subject: str
    client_id: str
    credential_issuer: str
    credential_subject: str
    base_revision: int
    candidate_revision: int
    expires_at: int
    card_content_hash: str  # the committed revision's content hash: the target incarnation fence
    # The candidate Card's own authority snapshot (declared resource keys), fixed at begin. The SDK
    # writes exactly these into both credential records and their envelopes; reserve compares them.
    operations: tuple[str, ...]
    resource_grants: Mapping[str, tuple[str, ...]]
    resource_operations: Mapping[str, tuple[str, ...]]
    # The sorted union of resource_grants' values: what readers take as the token's scopes and grants.
    scopes: tuple[str, ...]
    delivery_deadline: int
    reserved_until: int
    slots: tuple[str, ...]
    effect_digests: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["slots"] = list(self.slots)
        value["effect_digests"] = dict(self.effect_digests)
        value["operations"] = list(self.operations)
        value["scopes"] = list(self.scopes)
        for name in ("resource_grants", "resource_operations"):
            value[name] = {key: list(items) for key, items in getattr(self, name).items()}
        return value

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "OAuthIssuancePlan":
        """The plan from its stored or transmitted form; KeyError when a field is missing (an older plan)."""
        return cls(**{**{name: raw[name] for name in cls.__dataclass_fields__},
                      "slots": tuple(raw["slots"]), "effect_digests": dict(raw["effect_digests"]),
                      "operations": tuple(raw["operations"]), "scopes": tuple(raw["scopes"]),
                      **{name: {key: tuple(items) for key, items in dict(raw[name]).items()}
                         for name in ("resource_grants", "resource_operations")}})


@dataclass(frozen=True)
class SlotOutcome:
    outcome: str        # "applied" | "superseded" | "released" | "pending"
    effect_digest: str  # the plan's effect digest for this slot
    token_sha256: str   # the reserved credential's digest (never the bearer); "" if never reserved


@dataclass(frozen=True)
class OAuthIssuanceResult:
    """What the ONE decision did with the plan of the same ``transaction_id`` and ``intent_digest``."""

    transaction_id: str
    intent_digest: str
    state: str          # "committed" | "aborted" | "pending"
    access_id: str
    card_revision: int  # the committed revision; the base revision unless committed
    expires_at: int
    delivery_deadline: int
    receipt_digest: str  # the Hub's committed receipt digest; "" unless committed
    per_slot: Mapping[str, SlotOutcome]
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["per_slot"] = {slot: asdict(outcome) for slot, outcome in self.per_slot.items()}
        return value


__all__ = ["ISSUANCE_DELIVERY_SECONDS", "ISSUANCE_PLAN_SCHEMA", "ISSUANCE_SLOTS", "IssuanceRefused",
           "OAuthIssuancePlan", "OAuthIssuanceResult", "SlotOutcome", "credential_issue_effect",
           "decision_request_id", "effect_digest", "original_input_digest"]
