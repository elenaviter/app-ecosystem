# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W585: the Hub's side of a bound session issuance for a committed Card transaction.

The SDK (``kdcube_ai_app.auth.bundle``) issues or recovers one session per
committed ``grant_binding`` effect and keeps its bearer in create-only custody.
The SDK trusts the host for the context: "Hosts validate actor, intent, effect
and live target eligibility before this internal call. Request JSON never
supplies that authority." This module is that host validation:

- the actor is the committed GlobalIntent's Hub projection ``actor_subject``
  (W581 v2), with its ``actor_kind``; never the receipt, the payload or a caller;
- that intent's digest must equal the participant receipt's, so the effect
  belongs to exactly the transaction that was committed;
- the payload must reproduce the binding's effect digest exactly, so a
  deadline or slot cannot differ from what was decided (Ops CP1 F3);
- the target incarnation is the committed Card revision (``after``), an
  integer exactly one past ``before`` for the same Card (Ops CP1 F4).

Custody (W585 gates, Ops and Infra 2026-10-06 16:29 to 17:18): one namespace,
``connection-hub-issuance-custody``, only through Infra's exact
``KDCubeIssuanceSecretCustody`` wrapper (never the bare store, never a default
namespace), and only after ``await custody.qualify()`` confirms the configured
secrets backend meets the custody guarantees. The Hub never chooses or
refuses by backend name: the operator, 2026-10-06 17:44, verbatim: "secrets
service abstraction must work for all \"modes\". we have: secrets file, host
vault and also we have aws secrets manager for cloud deployment. therefore,
all 3 must be supported, based on what is configured." and "this is the
setting of the secrets service itself and the clients of secrets service
should not care how it is implemented." Nothing here is module state; the composition root passes
the decision store, the custody and its backend name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from .card_participant import hub_projection
from .participant_effects import EffectBinding, _canonical, _digest

# The exact namespace and policy selector (Infra 16:41, Ops 16:42): the SDK
# parser is ^[a-z0-9][a-z0-9-]{0,63}$, so no dots.
CUSTODY_NAMESPACE = "connection-hub-issuance-custody"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class BoundIssuanceRefused(ValueError):
    """A named, finite refusal; it never carries a credential or a payload value."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class HubIssuanceContext:
    """The fields of the SDK's ``IssuanceContext``; the SDK re-validates them."""

    tenant: str
    project: str
    transaction_id: str
    slot: str
    actor: str
    effect_digest: str
    receipt_digest: str
    access_id: str
    target_incarnation: int
    expires_at: int


async def build_issuance_context(
    *, binding: EffectBinding, payload: Mapping[str, Any], decisions: Any, tenant: str, project: str,
) -> HubIssuanceContext:
    """The trusted context for one committed ``grant_binding`` effect, or a named refusal."""

    if binding.kind != "grant_binding":
        raise BoundIssuanceRefused("issuance_effect_kind_invalid")
    if not (_HEX64.fullmatch(binding.transaction_id) and _HEX64.fullmatch(binding.effect_digest)
            and _HEX64.fullmatch(binding.receipt_digest)):
        raise BoundIssuanceRefused("issuance_binding_mismatch")
    # F3: the payload is exactly the decided effect, deadline included.
    if _digest(_canonical({"kind": binding.kind, "key": binding.key, "payload": dict(payload)})) != binding.effect_digest:
        raise BoundIssuanceRefused("issuance_binding_mismatch")
    receipt = binding.receipt()
    access_id = payload.get("access_id")
    if payload.get("slot") != binding.key or access_id != receipt.get("access_id"):
        raise BoundIssuanceRefused("issuance_binding_mismatch")
    expires_at = payload.get("expires_at")
    if type(expires_at) is not int or expires_at <= 0:
        raise BoundIssuanceRefused("issuance_deadline_invalid")
    # F4: the committed Card revision, exactly one past the base, for this Card.
    before, after = receipt.get("before") or {}, receipt.get("after") or {}
    incarnation = after.get("card_revision")
    if (type(incarnation) is not int or incarnation < 1 or type(before.get("card_revision")) is not int
            or incarnation != before["card_revision"] + 1 or after.get("access_id") != access_id):
        raise BoundIssuanceRefused("issuance_binding_mismatch")
    record = await decisions.read(binding.transaction_id)
    # Only a committed transaction issues; an undecided or aborted one never does.
    if record is None or not record.terminal or record.state != "committed":
        raise BoundIssuanceRefused("issuance_decision_not_committed")
    if record.transaction_id != binding.transaction_id or record.intent.digest != receipt.get("intent_digest"):
        raise BoundIssuanceRefused("issuance_intent_mismatch")
    # F1: the actor is the committed projection's, never a receipt or payload value.
    try:
        projection = hub_projection(record.intent)
    except Exception:  # noqa: BLE001 - a named refusal, never a raw kernel error
        raise BoundIssuanceRefused("issuance_intent_mismatch") from None
    actor = projection.get("actor_subject")
    if type(actor) is not str or not actor or projection.get("actor_kind") not in ("caller", "grantor"):
        raise BoundIssuanceRefused("issuance_actor_invalid")
    return HubIssuanceContext(
        tenant=tenant, project=project, transaction_id=binding.transaction_id, slot=binding.key,
        actor=actor, effect_digest=binding.effect_digest,
        receipt_digest=binding.receipt_digest, access_id=access_id,
        target_incarnation=incarnation, expires_at=expires_at,
    )


def integration_user_id(client_id: str, grantor_subject: str) -> str:
    """The bound session's subject: ``integration:<client>:<grantor>`` (W585)."""

    for value in (client_id, grantor_subject):
        if type(value) is not str or not value or value != value.strip() or any(c.isspace() for c in value):
            raise BoundIssuanceRefused("issuance_user_invalid")
    if ":" in client_id:
        raise BoundIssuanceRefused("issuance_user_invalid")
    return f"integration:{client_id}:{grantor_subject}"


def _issuance_custody_type() -> type | None:
    try:
        from kdcube_ai_app.infra.secrets.issuance import KDCubeIssuanceSecretCustody
    except ImportError:
        return None
    return KDCubeIssuanceSecretCustody


async def require_production_custody(custody: Any, *, production: bool) -> Any:
    """Refuse custody that cannot hold a credential until its deadline (W585 gates 2, 3).

    In production only Infra's exact ``KDCubeIssuanceSecretCustody`` (Ops CP1
    F2), in the one namespace (no default), and only after the secrets
    layer's own ``await custody.qualify()`` passes. Whether a backend meets the
    custody guarantees is the secrets layer's decision for whatever mode is
    configured (secrets file, host vault or AWS); the Hub has no backend
    allow-list (operator, 17:44). ``production`` has no default; the
    composition root names where it comes from.
    """

    if type(production) is not bool:
        raise BoundIssuanceRefused("issuance_custody_unavailable")
    if not all(callable(getattr(custody, name, None)) for name in ("create", "get")):
        raise BoundIssuanceRefused("issuance_custody_unavailable")
    if getattr(custody, "namespace", None) != CUSTODY_NAMESPACE:
        raise BoundIssuanceRefused("issuance_custody_namespace_invalid")
    if not production:
        return custody
    wrapper = _issuance_custody_type()
    if wrapper is None or type(custody) is not wrapper:
        raise BoundIssuanceRefused("issuance_custody_unqualified")
    try:
        await custody.qualify()
    except Exception:  # noqa: BLE001 - the wrapper's own named refusal; never its text
        raise BoundIssuanceRefused("issuance_custody_not_durable") from None
    return custody


__all__ = [
    "BoundIssuanceRefused", "CUSTODY_NAMESPACE", "HubIssuanceContext",
    "build_issuance_context", "integration_user_id", "require_production_custody",
]
