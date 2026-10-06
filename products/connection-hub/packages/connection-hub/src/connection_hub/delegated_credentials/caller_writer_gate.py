"""Caller-writer gate: a bound owner write is AUTHORIZED by its binding's policy (W578).

Scope, stated plainly: this is an authorization seam only. A decision here
plus a revalidation inside the durable commit closes the bypass of ordinary
owner writes; it is NOT a staged participant, a business-transaction
decision or a reader publication barrier, and it does not make a PB
membership change and a Card change visible atomically. That participant
(prepare/stage/decision/recover) is a separate generic Protocol.

An ordinary owner write (update, per-service reset, revoke, attach, detach)
of a Card that is bound to a Control persists today with no external policy:
only credentialless, issuer-managed Cards pass the issuer gate. This module
gives such a bound write the same discipline as the issuer gate, without
Connection Hub learning anything about the binding's application:

- the policy is selected by the record's trusted Control binding
  (``control_card.issuer_kind``), never by a name the caller supplies;
- the request binds the authenticated hosting actor (never the storage owner),
  the action, the target, its expected revision and the digest of the
  complete server-built candidate;
- the policy decides before the write, revalidates inside the durable commit
  (``persist_guarded`` / ``forget_guarded`` call the gate under the target
  lock, after their revision check), and finalizes the outcome after it;
- a bound record whose policy is missing, refuses, expires or answers for
  another request is refused; an unbound record keeps its existing path.

The policy owns its business rules (PB: role, membership, last usable admin,
its transaction participant). Connection Hub carries no roles, labels or
counts here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Protocol

from .issuer_gate import change_digest

# W580/B (CodeApp, 11:33): EVERY writer of a bound Card names its action and
# is decided by the binding's policy; expiry is governed too. The per-action
# shape rules below are generic: they bound what an action may change, and
# the policy decides whether it may happen. No PB semantics live here.
SYSTEM_WRITE_ACTIONS = ("create", "replace", "extend", "oauth_grant", "renew", "prolong", "prune", "fold",
                        "control_snapshot")
CALLER_WRITE_ACTIONS = ("update", "reset", "revoke", "attach", "detach", *SYSTEM_WRITE_ACTIONS)
# The candidate fields a caller write may never change: they are the target's
# identity, its credentials' subject and its expiry.
PROTECTED_FIELDS = ("access_id", "grantor_subject", "delegate_subject", "client_id", "issuer_kind",
                    "issuer_ref", "source", "card_kind", "expires_at")
# A Card's identity: no action but create may change it.
IDENTITY_FIELDS = tuple(name for name in PROTECTED_FIELDS if name != "expires_at")
# What a prolongation may change: only its expiry, never backward, and its bookkeeping.
PROLONG_FIELDS = ("expires_at", "provenance", "card_revision")
PRUNE_FIELDS = ("account_scope", "card_revision")


def _scope_subset(after: Any, before: Any) -> bool:
    if not isinstance(after, Mapping) or not isinstance(before, Mapping):
        return False
    for provider, accounts in after.items():
        held = before.get(provider)
        if not isinstance(accounts, Mapping) or not isinstance(held, Mapping):
            return False
        for account, claims in accounts.items():
            if account not in held or not set(claims or ()) <= set(held.get(account) or ()):
                return False
    return True


def _binding_of_mapping(card: Mapping[str, Any] | None) -> tuple[str, str]:
    control = card.get("control_card") if isinstance(card, Mapping) else None
    if not isinstance(control, Mapping):
        return "", ""
    return str(control.get("issuer_kind") or ""), str(control.get("issuer_ref") or "")


def binding_change_refusal(action: str, before: Mapping[str, Any] | None, candidate: Mapping[str, Any]) -> str | None:
    """The Control binding is pinned (Ops B1, 12:14): only attach binds and only detach unbinds.

    Otherwise one approved write could turn a governed Card into an ungoverned
    one (or rebind it to another issuer), and every later write would skip
    its gate. attach binds an unbound Card, or keeps its issuer (another
    Control of the same issuer); it never moves a Card to another issuer.
    detach leaves it unbound. A revoke publishes only the revoked state.
    """

    after_binding = _binding_of_mapping(candidate)
    before_binding = _binding_of_mapping(before)
    if action in ("create", "revoke"):
        return None
    if action == "attach":
        return (None if after_binding[0] and before_binding in (("", ""), after_binding)
                else "caller_writer_binding_change_refused")
    if action == "detach":
        return None if after_binding == ("", "") else "caller_writer_binding_change_refused"
    return None if after_binding == before_binding else "caller_writer_binding_change_refused"


def candidate_shape_refusal(action: str, before: Mapping[str, Any] | None, candidate: Mapping[str, Any]) -> str | None:
    """What this action may change, checked before the policy is asked; None when the shape holds."""

    binding = binding_change_refusal(action, before, candidate)
    if binding is not None:
        return binding
    if action == "create":
        return None if before is None else "caller_writer_candidate_binding_mismatch"
    if before is None or candidate.get("card_revision") != before.get("card_revision", 0) + 1:
        return "caller_writer_candidate_binding_mismatch"
    if action in ("update", "reset"):
        return ("caller_writer_candidate_binding_mismatch"
                if any(candidate.get(name) != before.get(name) for name in PROTECTED_FIELDS) else None)
    if any(candidate.get(name) != before.get(name) for name in IDENTITY_FIELDS):
        return "caller_writer_candidate_binding_mismatch"
    changed = {name for name in set(before) | set(candidate) if candidate.get(name) != before.get(name)}
    if action == "prolong":
        if not changed <= set(PROLONG_FIELDS) or int(candidate.get("expires_at") or 0) < int(before.get("expires_at") or 0):
            return "caller_writer_prolong_shape_invalid"
    elif action == "prune":
        if not changed <= set(PRUNE_FIELDS) or not _scope_subset(candidate.get("account_scope") or {},
                                                                before.get("account_scope") or {}):
            return "caller_writer_prune_shape_invalid"
    return None


class CallerWriteRefused(ValueError):
    """A bound write the policy did not authorize; the reason is a bounded code."""

    def __init__(self, reason: str, *, outcome_confirmed: bool | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.outcome_confirmed = outcome_confirmed

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"ok": False, "status": 403, "error": self.reason}
        if self.outcome_confirmed is not None:
            result["caller_write_outcome_confirmed"] = self.outcome_confirmed
        return result


@dataclass(frozen=True)
class CallerWriteRequest:
    actor_subject: str
    request_id: str
    action: str
    access_id: str
    card_revision: int
    issuer_kind: str
    issuer_ref: str
    binding_kind: str
    binding_ref: str
    change_digest: str
    context_ref: str = ""


@dataclass(frozen=True)
class CallerWrite:
    """How one writer enlists its write of a possibly bound Card: its action and authenticated actor."""

    action: str
    actor_subject: str
    request_id: str = ""
    context_ref: str = ""


@dataclass(frozen=True)
class CallerWriteDecision:
    allowed: bool
    reason: str
    policy_version: str
    valid_until: datetime
    request: CallerWriteRequest | None = None


class CallerWriterPolicy(Protocol):
    async def decide(self, request: CallerWriteRequest) -> CallerWriteDecision: ...
    async def revalidate(self, request: CallerWriteRequest, initial: CallerWriteDecision) -> CallerWriteDecision: ...
    async def finalize(self, request: CallerWriteRequest, *, state: str, card_revision: int) -> bool: ...


class CallerWriterRegistry:
    """Policies by Control binding kind; registered once by the hosting composition."""

    def __init__(self) -> None:
        self._policies: dict[str, CallerWriterPolicy] = {}
        self._required: set[str] = set()

    def require(self, binding_kind: str) -> None:
        """Declare a trusted binding kind whose writes must have a policy: missing means refused."""
        kind = str(binding_kind or "").strip()
        if not kind:
            raise ValueError("caller_writer_policy_registration_invalid")
        self._required.add(kind)

    def register(self, binding_kind: str, policy: CallerWriterPolicy) -> None:
        kind = str(binding_kind or "").strip()
        if not kind or kind in self._policies or not all(
            callable(getattr(policy, name, None)) for name in ("decide", "revalidate", "finalize")
        ):
            raise ValueError("caller_writer_policy_registration_invalid")
        self._policies[kind] = policy

    def is_bound(self, binding_kind: str) -> bool:
        kind = str(binding_kind or "")
        return kind in self._policies or kind in self._required

    def policy_for(self, binding_kind: str) -> CallerWriterPolicy | None:
        return self._policies.get(str(binding_kind or ""))


def binding_of(authority: Any) -> tuple[str, str]:
    """The record's trusted Control binding, or empty strings when it has none."""

    control = getattr(authority, "control_card", None)
    if control is None:
        return "", ""
    return str(getattr(control, "issuer_kind", "") or ""), str(getattr(control, "issuer_ref", "") or "")


def caller_write_refusal(request: CallerWriteRequest, decision: object, *, now: datetime) -> str | None:
    """Exact binding and expiry check for one decision; None when it authorizes this request."""

    if not isinstance(decision, CallerWriteDecision):
        return "caller_writer_decision_invalid"
    if decision.allowed is not True:
        return decision.reason or "caller_writer_refused"
    if decision.request is not None and decision.request != request:
        return "caller_writer_decision_mismatch"
    valid_until = decision.valid_until
    if not isinstance(valid_until, datetime) or valid_until.tzinfo is None or now >= valid_until:
        return "caller_writer_decision_expired"
    if not decision.policy_version:
        return "caller_writer_decision_invalid"
    return None


async def caller_writer_before_commit(
    registry: CallerWriterRegistry | None,
    current: Any,
    *,
    actor_subject: str,
    action: str,
    candidate: Mapping[str, Any],
    request_id: str,
    context_ref: str = "",
    now: Callable[[], datetime] | None = None,
    binding: tuple[str, str] | None = None,
) -> tuple[Callable[[], Awaitable[None]] | None, CallerWriteRequest | None]:
    """The commit gate for one bound write, or (None, None) when the Card has no registered binding.

    ``current`` is the stored Card authority; ``candidate`` the complete
    server-built next state (for revoke, attach and detach, the exact change
    the write publishes). Raises ``CallerWriteRefused`` before any effect.
    """

    clock = now or (lambda: datetime.now(timezone.utc))
    # An attach to a Card with no binding yet is governed by the binding it
    # is about to take; the caller passes it explicitly.
    binding_kind, binding_ref = binding if binding is not None else binding_of(current)
    if not binding_kind or registry is None or not registry.is_bound(binding_kind):
        return None, None
    policy = registry.policy_for(binding_kind)
    if policy is None:
        raise CallerWriteRefused("caller_writer_policy_unavailable")
    if action not in CALLER_WRITE_ACTIONS:
        raise CallerWriteRefused("caller_writer_action_invalid")
    subject = str(actor_subject or "").strip()
    if not subject or subject == "anonymous" or subject.startswith(("integration:", "telegram_")):
        raise CallerWriteRefused("caller_writer_requires_authenticated_actor")
    if action == "create":
        # A new bound Card: there is no stored target, so the binding is the
        # one the candidate takes (the caller passes it) and the revision is 0.
        if current is not None or binding is None:
            raise CallerWriteRefused("caller_writer_target_invalid")
        revision = 0
    else:
        revision = getattr(current, "card_revision", None)
        if type(revision) is not int or revision < 1:
            raise CallerWriteRefused("caller_writer_target_invalid")
    before = (current.to_dict() if callable(getattr(current, "to_dict", None)) else {}) if current is not None else None
    shape = (candidate_shape_refusal(action, before, candidate) if action not in ("revoke", "attach", "detach")
             else binding_change_refusal(action, before, candidate))
    if shape is not None:
        raise CallerWriteRefused(shape)
    request = CallerWriteRequest(
        actor_subject=subject, request_id=str(request_id or "").strip(), action=action,
        access_id=str(getattr(current, "access_id", "") or candidate.get("access_id") or ""), card_revision=revision,
        issuer_kind=str(getattr(current, "issuer_kind", "") or "") if current is not None else str(candidate.get("issuer_kind") or ""),
        issuer_ref=str(getattr(current, "issuer_ref", "") or "") if current is not None else str(candidate.get("issuer_ref") or ""),
        binding_kind=binding_kind, binding_ref=binding_ref,
        change_digest=change_digest(dict(candidate)), context_ref=str(context_ref or "").strip(),
    )
    if not request.request_id:
        raise CallerWriteRefused("caller_writer_request_id_required")
    try:
        decision = await policy.decide(request)
    except CallerWriteRefused:
        raise
    except Exception as exc:  # noqa: BLE001 - an unanswered policy authorizes nothing
        raise CallerWriteRefused("caller_writer_policy_unavailable") from exc
    refusal = caller_write_refusal(request, decision, now=clock())
    if refusal is not None:
        confirmed = await _finalize(policy, request, state="refused", card_revision=revision)
        raise CallerWriteRefused(refusal, outcome_confirmed=confirmed)

    async def before_commit() -> None:
        # Called by the durable store INSIDE the target lock, after its own
        # revision check and before any effect: the decision must still hold.
        try:
            fresh = await policy.revalidate(request, decision)
        except Exception as exc:  # noqa: BLE001
            raise CallerWriteRefused("caller_writer_policy_unavailable") from exc
        reason = caller_write_refusal(request, fresh, now=clock())
        if reason is not None:
            raise CallerWriteRefused(reason)

    return before_commit, request


def reset_candidate(current: Any, *, resource: str, control_operations: Any, control_grants: Any,
                    control_named_services: Any = None) -> dict[str, Any]:
    """Explicit per-service Reset to Control: one service's selection becomes the current Control's.

    ``control_operations`` and ``control_grants`` are that service's current
    effective Control selection, resolved by the upstream-owned hierarchy
    (W577's AND/OR), never read from the stored personal Card. Every other
    service, the Card's identity and its credentials are byte-identical; only
    this resource's operations and grants and the revision change. Nothing
    calls this on a read or on a Control change: it is the owner's explicit
    action, decided like any other bound write.
    """

    resource = str(resource or "").strip()
    if not resource:
        raise CallerWriteRefused("caller_writer_reset_resource_required")
    operations = tuple(sorted({str(op) for op in control_operations}))
    grants = tuple(sorted({str(grant) for grant in control_grants}))
    selected_operations = dict(getattr(current, "resource_operations", {}) or {})
    selected_grants = dict(getattr(current, "resource_grants", {}) or {})
    if resource not in selected_operations and resource not in selected_grants:
        raise CallerWriteRefused("caller_writer_reset_service_not_held")
    selected_operations[resource] = operations
    selected_grants[resource] = grants
    import dataclasses

    changes: dict[str, Any] = {"resource_operations": selected_operations, "resource_grants": selected_grants}
    # The service's complete authority: its named-service selection follows the
    # Control's too (Infra, 11:02), only for this resource's entry.
    named = getattr(current, "named_service_operations", None)
    if control_named_services is not None and named is not None and not (named.is_all or named.is_unknown):
        entries = {key: value for key, value in dict(named.operations).items() if key != resource}
        if control_named_services:
            entries[resource] = control_named_services
        changes["named_service_operations"] = type(named).exact(entries)
    reset = dataclasses.replace(current, card_revision=current.card_revision + 1, **changes)
    before, after = current.to_dict(), reset.to_dict()
    # Only the one service's selection (and the derived flat operation union)
    # and the revision may differ; anything else is a construction error.
    allowed = {"card_revision", "operations", "resource_operations", "resource_grants", "named_service_operations"}
    if any(before.get(key) != after.get(key) for key in set(before) | set(after) if key not in allowed):
        raise CallerWriteRefused("caller_writer_reset_preservation_failed")
    for dimension in ("resource_operations", "resource_grants"):
        for other in set(before.get(dimension) or {}) | set(after.get(dimension) or {}):
            if other != resource and (before.get(dimension) or {}).get(other) != (after.get(dimension) or {}).get(other):
                raise CallerWriteRefused("caller_writer_reset_preservation_failed")
    return after


async def caller_write_outcome(registry: CallerWriterRegistry | None, request: CallerWriteRequest | None, *,
                               state: str, card_revision: int) -> dict[str, Any]:
    """Report the write's terminal outcome to its policy; {} when the write was not gated."""

    if request is None or registry is None:
        return {}
    policy = registry.policy_for(request.binding_kind)
    if policy is None:
        return {"caller_write_outcome_confirmed": False}
    return {"caller_write_outcome_confirmed": await _finalize(policy, request, state=state,
                                                              card_revision=card_revision)}


async def _finalize(policy: CallerWriterPolicy, request: CallerWriteRequest, *, state: str, card_revision: int) -> bool:
    try:
        return bool(await policy.finalize(request, state=state, card_revision=card_revision))
    except Exception:  # noqa: BLE001 - an unconfirmed outcome is reported, never assumed
        return False


__all__ = [
    "CALLER_WRITE_ACTIONS", "CallerWrite", "CallerWriteDecision", "CallerWriteRefused", "CallerWriteRequest",
    "CallerWriterPolicy", "CallerWriterRegistry", "IDENTITY_FIELDS", "PROTECTED_FIELDS", "SYSTEM_WRITE_ACTIONS",
    "binding_change_refusal", "binding_of", "candidate_shape_refusal",
    "caller_write_outcome", "caller_write_refusal", "caller_writer_before_commit", "reset_candidate",
]
