"""Opaque issuer authorization for externally managed, credentialless Cards.

Policy lives behind a trusted adapter, not in the Card service. Decisions are
in-process receipts, never client JSON. A receipt binds one complete request
and must be freshly revalidated immediately before the target revision CAS.
That check does not claim a transaction with the issuer's authority store.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Literal, Mapping, Protocol

MAX_DECISION_SECONDS = 60


class IssuerWriteRefused(RuntimeError):
    def __init__(self, reason: str, *, outcome_confirmed: bool | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.outcome_confirmed = outcome_confirmed

    def to_dict(self) -> dict[str, Any]:
        outcome = {} if self.outcome_confirmed is None else {"issuer_outcome_confirmed": self.outcome_confirmed}
        return {"ok": False, "error": "issuer_managed_card", "reason": self.reason, "status": 403, **outcome}


def change_digest(change: Mapping[str, Any]) -> str:
    """Hash the actual server-built mutation, without secrets or client proofs."""
    wire = json.dumps(change, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(wire.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IssuerRequest:
    actor_subject: str
    request_id: str
    action: Literal["update", "revoke"]
    access_id: str
    card_revision: int
    issuer_kind: str
    issuer_ref: str
    change_digest: str
    context_ref: str


@dataclass(frozen=True)
class IssuerDecision:
    request: IssuerRequest
    allowed: bool
    reason: str
    policy_version: str
    valid_until: datetime
    adapter_id: str
    _seal: object = field(default=None, repr=False, compare=False)


class IssuerAdapter(Protocol):
    issuer_kind: str
    adapter_id: str

    async def decide(self, request: IssuerRequest) -> tuple[bool, str, str, datetime]:
        """Fresh, non-consuming policy read; no same-request result memo."""
        ...


@dataclass(frozen=True)
class _Proof:
    registry: object
    adapter: object
    request: IssuerRequest
    allowed: bool
    reason: str
    policy_version: str
    valid_until: datetime
    adapter_id: str


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.utcoffset() is not None


def _request_valid(request: object, *, allow_empty_context: bool = False) -> bool:
    return (
        type(request) is IssuerRequest
        and request.action in ("update", "revoke")
        and type(request.card_revision) is int
        and request.card_revision > 0
        and all(type(value) is str and bool(value.strip()) for value in (
            request.actor_subject, request.request_id, request.access_id,
            request.issuer_kind, request.issuer_ref,
        ))
        and type(request.context_ref) is str
        and (allow_empty_context or bool(request.context_ref.strip()))
        and type(request.change_digest) is str
        and len(request.change_digest) == 64
        and all(c in "0123456789abcdef" for c in request.change_digest)
    )


def _authentic(decision: object, *, registry: object | None = None,
               adapter: object | None = None) -> bool:
    if type(decision) is not IssuerDecision or type(decision._seal) is not _Proof:
        return False
    seal = decision._seal
    return (
        (registry is None or seal.registry is registry)
        and (adapter is None or seal.adapter is adapter)
        and type(decision.request) is IssuerRequest
        and type(decision.allowed) is bool
        and (decision.request, decision.allowed, decision.reason,
             decision.policy_version, decision.valid_until, decision.adapter_id)
        == (seal.request, seal.allowed, seal.reason, seal.policy_version,
            seal.valid_until, seal.adapter_id)
    )


def issuer_write_refusal(record: Any, request: IssuerRequest,
                         decision: object, *, now: datetime) -> dict[str, Any] | None:
    """Synchronous exact binding/expiry check, directly before durable CAS.

    The caller invokes this only for a managed credentialless Card. It must
    also use its own registry to revalidate, so a different registry cannot
    supply a receipt merely by implementing the same public adapter id.
    """
    reason = ""
    if not _request_valid(request):
        reason = "issuer_request_invalid"
    elif not _authentic(decision):
        reason = "issuer_decision_unsealed"
    elif decision.request != request:
        reason = "issuer_request_mismatch"
    elif (request.access_id, request.card_revision, request.issuer_kind,
          request.issuer_ref) != (record.access_id, record.card_revision,
                                  record.issuer_kind, record.issuer_ref):
        reason = "issuer_card_binding_mismatch"
    elif not decision.allowed:
        reason = decision.reason or "issuer_refused"
    elif not _aware(now) or not _aware(decision.valid_until):
        reason = "issuer_decision_expiry_invalid"
    elif now >= decision.valid_until:
        reason = "issuer_decision_expired"
    if not reason:
        return None
    return {"ok": False, "error": "issuer_managed_card", "reason": reason,
            "status": 403}


class IssuerRegistry:
    """Trusted composition registration; unknown required issuers fail closed.

    Explicit owner-managed kinds preserve generic owner/Card behavior. A
    registered adapter overrides that exception; removing it must not turn
    a formerly registered kind into an owner-only authorization path.
    """

    def __init__(self, *, owner_managed_kinds: Iterable[str] = (),
                 now: Callable[[], datetime] | None = None) -> None:
        self._adapters: dict[str, IssuerAdapter] = {}
        self._required: set[str] = set()
        self._owner_managed = frozenset(owner_managed_kinds)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._identity = object()

    def register(self, adapter: IssuerAdapter) -> None:
        if not isinstance(adapter.issuer_kind, str) or not adapter.issuer_kind.strip():
            raise ValueError("issuer_kind_invalid")
        if not isinstance(adapter.adapter_id, str) or not adapter.adapter_id.strip():
            raise ValueError("issuer_adapter_id_invalid")
        if not callable(getattr(adapter, "decide", None)):
            raise ValueError("issuer_adapter_invalid")
        self._required.add(adapter.issuer_kind)
        self._adapters[adapter.issuer_kind] = adapter

    def is_managed(self, issuer_kind: str) -> bool:
        return issuer_kind in self._required or issuer_kind not in self._owner_managed

    async def prepare(self, request: IssuerRequest, *, current: Mapping[str, Any],
                      candidate: Mapping[str, Any]) -> IssuerRequest:
        """Trusted server-built candidate -> opaque authority-owned reservation.

        Optional adapter capability, not a client-input policy API. Preparing
        a reservation supplies evidence, never authorizes a target write.
        """
        if (not _request_valid(request, allow_empty_context=True)
                or change_digest(candidate) != request.change_digest):
            raise IssuerWriteRefused("issuer_candidate_invalid")
        adapter = self._adapters.get(request.issuer_kind)
        prepare = getattr(adapter, "prepare_context", None)
        if not callable(prepare):
            raise IssuerWriteRefused("issuer_context_provider_unavailable")
        try:
            context = await prepare(request, current=current, candidate=candidate)
            if self._adapters.get(request.issuer_kind) is not adapter:
                raise ValueError("issuer_adapter_changed")
            prepared = replace(request, context_ref=context)
            if not _request_valid(prepared):
                raise ValueError("issuer_context_invalid")
            return prepared
        except Exception as exc:
            raise IssuerWriteRefused("issuer_context_provider_unavailable") from exc

    async def finalize(self, request: IssuerRequest, *, state: str,
                       card_revision: int) -> bool:
        """Record a terminal outcome externally; failure cannot undo a commit."""
        adapter = self._adapters.get(request.issuer_kind)
        finalize = getattr(adapter, "finalize_context", None)
        if not callable(finalize):
            return False
        try:
            return await finalize(request, outcome={"state": state, "card_revision": card_revision}) is True
        except Exception:
            return False

    def _receipt(self, request: IssuerRequest, adapter: object, allowed: bool,
                 reason: str, version: str, until: datetime, adapter_id: str) -> IssuerDecision:
        seal = _Proof(self._identity, adapter, request, allowed, reason,
                      version, until, adapter_id)
        return IssuerDecision(request, allowed, reason, version, until, adapter_id, seal)

    def _refuse(self, request: IssuerRequest, reason: str) -> IssuerDecision:
        return self._receipt(request, None, False, reason, "", self._now(), "")

    async def decide(self, request: IssuerRequest) -> IssuerDecision:
        if not _request_valid(request):
            return self._refuse(request, "issuer_request_invalid")
        adapter = self._adapters.get(request.issuer_kind)
        if adapter is None:
            return self._refuse(request, "issuer_adapter_unavailable")
        adapter_id = adapter.adapter_id
        try:
            result = await adapter.decide(request)
            if not isinstance(result, tuple) or len(result) != 4:
                return self._refuse(request, "issuer_response_invalid")
            allowed, reason, version, until = result
            now = self._now()
            if (type(allowed) is not bool or type(reason) is not str
                    or type(version) is not str or not version.strip()
                    or not _aware(now) or not _aware(until)):
                return self._refuse(request, "issuer_response_invalid")
            if until <= now:
                return self._refuse(request, "issuer_decision_expired")
            if until > now + timedelta(seconds=MAX_DECISION_SECONDS):
                return self._refuse(request, "issuer_decision_window_exceeded")
            # Registration or adapter identity may change across the await.
            if (self._adapters.get(request.issuer_kind) is not adapter
                    or adapter.adapter_id != adapter_id):
                return self._refuse(request, "issuer_adapter_changed")
            return self._receipt(request, adapter, allowed, reason, version, until, adapter_id)
        except Exception:
            # Never expose transport messages or policy/credential internals.
            return self._refuse(request, "issuer_adapter_unavailable")

    async def revalidate(self, request: IssuerRequest,
                         decision: object) -> IssuerDecision:
        adapter = self._adapters.get(request.issuer_kind)
        if not _authentic(decision, registry=self._identity, adapter=adapter):
            return self._refuse(request, "issuer_decision_unsealed")
        if decision.request != request:
            return self._refuse(request, "issuer_request_mismatch")
        if not decision.allowed:
            return decision
        now = self._now()
        if not _aware(now) or not _aware(decision.valid_until) or now >= decision.valid_until:
            return self._refuse(request, "issuer_decision_expired")
        fresh = await self.decide(request)
        if not fresh.allowed:
            return fresh
        if fresh.adapter_id != decision.adapter_id:
            return self._refuse(request, "issuer_adapter_changed")
        if fresh.policy_version != decision.policy_version:
            return self._refuse(request, "issuer_policy_changed")
        # Revalidation cannot extend the original authorization window.
        return self._receipt(request, adapter, True, fresh.reason,
                             fresh.policy_version,
                             min(fresh.valid_until, decision.valid_until),
                             fresh.adapter_id)
