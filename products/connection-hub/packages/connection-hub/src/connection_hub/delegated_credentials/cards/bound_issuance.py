# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W585: the Hub's side of a bound session issuance for a committed Card transaction.

The SDK (``kdcube_ai_app.auth.bundle``) issues or recovers one session per
committed ``grant_binding`` effect and keeps its bearer in create-only custody.
The SDK trusts the host for the context: "Hosts validate actor, intent, effect
and live target eligibility before this internal call. Request JSON never
supplies that authority." This module is that host validation:

- the actor comes from the coordinator's COMMITTED decision record (the W581
  kernel ``Intent``), never from the receipt, the payload or a caller;
- that record's intent digest must equal the participant receipt's, so the
  effect belongs to exactly the transaction that was committed;
- the target incarnation is the committed Card revision (``after``);
- the slot, access and deadline are the helper-validated effect payload's.

Custody (W585 gates, Ops and Infra 2026-10-06 16:29 to 16:46): one namespace,
``connection-hub-issuance-custody``, through Infra's envelope wrapper over the
platform secret store, never the bare store, and in production only on a
durable backend. Nothing here is module state; the composition root passes
the decision store, the custody and its backend name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from .participant_effects import EffectBinding

# The exact namespace and policy selector (Infra 16:41, Ops 16:42): the SDK
# parser is ^[a-z0-9][a-z0-9-]{0,63}$, so no dots.
CUSTODY_NAMESPACE = "connection-hub-issuance-custody"
# Backends that keep custody until expires_at across restarts. secrets-service
# counts only with the host-vault backend, never the temporary sidecar (Ops
# and Infra, 16:39); in-memory never does.
DURABLE_CUSTODY_BACKENDS = frozenset({"aws-sm", "host-vault"})

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
    receipt = binding.receipt()
    access_id = payload.get("access_id")
    if payload.get("slot") != binding.key or access_id != receipt.get("access_id"):
        raise BoundIssuanceRefused("issuance_binding_mismatch")
    expires_at = payload.get("expires_at")
    if type(expires_at) is not int or expires_at <= 0:
        raise BoundIssuanceRefused("issuance_deadline_invalid")
    record = await decisions.read(binding.transaction_id)
    # Only a committed transaction issues; an undecided or aborted one never does.
    if record is None or not record.terminal or record.state != "committed":
        raise BoundIssuanceRefused("issuance_decision_not_committed")
    if record.transaction_id != binding.transaction_id or record.intent.digest != receipt.get("intent_digest"):
        raise BoundIssuanceRefused("issuance_intent_mismatch")
    incarnation = int((receipt.get("after") or {}).get("card_revision") or 0)
    if incarnation < 1:
        raise BoundIssuanceRefused("issuance_binding_mismatch")
    if not (_HEX64.fullmatch(binding.transaction_id) and _HEX64.fullmatch(binding.effect_digest)
            and _HEX64.fullmatch(binding.receipt_digest)):
        raise BoundIssuanceRefused("issuance_binding_mismatch")
    return HubIssuanceContext(
        tenant=tenant, project=project, transaction_id=binding.transaction_id, slot=binding.key,
        actor=record.intent.actor, effect_digest=binding.effect_digest,
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


def require_production_custody(custody: Any, *, backend: str, production: bool) -> Any:
    """Refuse custody that cannot hold a credential until its deadline (W585 gate 3).

    Defence in depth beside the SDK's durability-required factory: the Hub
    never composes in-memory or sidecar custody for production, and only the
    one namespace. ``backend`` is the configured backend, not the provider name.
    """

    if not all(callable(getattr(custody, name, None)) for name in ("create", "get")):
        raise BoundIssuanceRefused("issuance_custody_unavailable")
    namespace = getattr(custody, "namespace", CUSTODY_NAMESPACE)
    if namespace != CUSTODY_NAMESPACE:
        raise BoundIssuanceRefused("issuance_custody_namespace_invalid")
    if production and backend not in DURABLE_CUSTODY_BACKENDS:
        raise BoundIssuanceRefused("issuance_custody_not_durable")
    return custody


__all__ = [
    "BoundIssuanceRefused", "CUSTODY_NAMESPACE", "DURABLE_CUSTODY_BACKENDS", "HubIssuanceContext",
    "build_issuance_context", "integration_user_id", "require_production_custody",
]
