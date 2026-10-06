"""Protected identity discovery, deliberately separate from issuer write seals.

The host binds the actual platform human and tenant/project, configured peers
authorize opaque coordinates, and the Card service reads both under its ordered
fences. Peer I/O runs before and after, NEVER inside those fences. Verification
authenticates a wire proof only: the recipient MUST atomically consume its nonce
in a durable, shared store before policy evaluation (including across processes).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping

from .admission import AdmissionRequest, ServiceProof, ServiceProofDecision, sign_admission_request, verify_admission_request

ISSUER_READ_PROTOCOL = "issuer-read.v1"
MAX_READ_SECONDS = 60
BASE_IDENTITY_FIELDS = ("access_id", "grantor_subject", "delegate_subject", "source", "card_kind",
    "card_revision", "state", "issuer_kind", "issuer_ref", "composition_mode", "identity_scope")
CONTROL_IDENTITY_FIELDS = ("control_id", "issuer_kind", "issuer_ref")


class IssuerReadRefused(RuntimeError):
    def __init__(self, reason: str, *, retryable: bool = False) -> None:
        super().__init__(reason)
        self.reason, self.retryable = reason, retryable


def read_digest(value: Any) -> str:
    """Literal UTF-8 canonical digest; not a write change_digest or wire seal."""
    wire = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(wire.encode("utf-8")).hexdigest()


def _text(value: Any, limit: int = 1024) -> bool:
    return type(value) is str and bool(value.strip()) and len(value.encode("utf-8")) <= limit


def _hash(value: Any) -> bool:
    return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


@dataclass(frozen=True)
class IssuerReadTarget:
    owner_subject: str
    access_id: str
    issuer_kind: str
    issuer_ref: str

    @property
    def subject_hash(self) -> str:
        return hashlib.sha256(self.owner_subject.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, str]:
        return {k: getattr(self, k) for k in ("owner_subject", "access_id", "issuer_kind", "issuer_ref")}

    @classmethod
    def from_mapping(cls, raw: Any) -> IssuerReadTarget:
        if not isinstance(raw, Mapping) or set(raw) != {"owner_subject", "access_id", "issuer_kind", "issuer_ref"}:
            raise IssuerReadRefused("issuer_read_target_invalid")
        if not all(_text(v) for v in raw.values()):
            raise IssuerReadRefused("issuer_read_target_invalid")
        return cls(**dict(raw))


@dataclass(frozen=True)
class IssuerReadQuery:
    context_ref: str
    request_id: str
    targets: tuple[IssuerReadTarget, IssuerReadTarget]

    def to_dict(self) -> dict[str, Any]:
        return {"context_ref": self.context_ref, "request_id": self.request_id,
                "targets": [t.to_dict() for t in self.targets]}

    @classmethod
    def from_mapping(cls, raw: Any) -> IssuerReadQuery:
        if not isinstance(raw, Mapping) or set(raw) != {"context_ref", "request_id", "targets"}:
            raise IssuerReadRefused("issuer_read_query_invalid")
        if not _text(raw["request_id"], 256) or not _text(raw["context_ref"], 4096):
            raise IssuerReadRefused("issuer_read_query_invalid")
        try:
            context = json.loads(raw["context_ref"])
            canonical = json.dumps(context, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
            if not isinstance(context, dict) or canonical != raw["context_ref"]:
                raise ValueError()
        except (TypeError, ValueError):
            raise IssuerReadRefused("issuer_read_context_invalid") from None
        rows = raw["targets"]
        if not isinstance(rows, (list, tuple)) or len(rows) != 2:
            raise IssuerReadRefused("issuer_read_pair_required")
        targets = tuple(IssuerReadTarget.from_mapping(t) for t in rows)
        if targets[0].access_id == targets[1].access_id:
            raise IssuerReadRefused("issuer_read_duplicate_target")
        targets = tuple(sorted(targets, key=lambda t: (t.owner_subject, t.access_id)))
        return cls(raw["context_ref"], raw["request_id"], targets)


@dataclass(frozen=True)
class IssuerReadRequest:
    actor_subject: str
    actor_classification: str
    tenant: str
    project: str
    context_ref: str
    request_id: str
    targets: tuple[IssuerReadTarget, IssuerReadTarget]

    def payload(self) -> dict[str, Any]:
        return {"actor_subject": self.actor_subject, "actor_classification": self.actor_classification,
                "tenant": self.tenant, "project": self.project,
                **IssuerReadQuery(self.context_ref, self.request_id, self.targets).to_dict()}

    @property
    def read_digest(self) -> str:
        return read_digest({"protocol": ISSUER_READ_PROTOCOL, "request": self.payload()})

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload(), "read_digest": self.read_digest}


def issuer_read_request_from_mapping(raw: Any) -> IssuerReadRequest:
    keys = {"actor_subject", "actor_classification", "tenant", "project", "context_ref", "request_id", "targets", "read_digest"}
    if not isinstance(raw, Mapping) or set(raw) != keys:
        raise IssuerReadRefused("issuer_read_request_invalid")
    query = IssuerReadQuery.from_mapping({k: raw[k] for k in ("context_ref", "request_id", "targets")})
    if (not all(_text(raw[k]) for k in ("actor_subject", "tenant", "project"))
            or raw["actor_classification"] not in ("registered", "privileged")
            or raw["actor_subject"] == "anonymous"
            or raw["actor_subject"].startswith(("integration:", "telegram_"))):
        raise IssuerReadRefused("issuer_read_requires_platform_human")
    request = IssuerReadRequest(raw["actor_subject"], raw["actor_classification"], raw["tenant"], raw["project"],
                                query.context_ref, query.request_id, query.targets)
    if raw["targets"] != request.to_dict()["targets"] or raw["read_digest"] != request.read_digest:
        raise IssuerReadRefused("issuer_read_request_binding_invalid")
    return request


def _request_valid(request: Any) -> bool:
    try:
        return type(request) is IssuerReadRequest and issuer_read_request_from_mapping(request.to_dict()) == request
    except (IssuerReadRefused, TypeError, ValueError, AttributeError):
        return False


def project_identity(authority: Any, leaf_paths: tuple[tuple[str, ...], ...] = ()) -> dict[str, Any]:
    """Fixed safe base plus trusted descriptor-selected scalar identity leaves.

    The host descriptor is the approval boundary for additional leaves. Callers
    cannot select exports. Never copy a parent mapping or unknown nested keys.
    Full fingerprints are calculated from the ORIGINAL authority, not this view.
    """
    raw = authority.to_dict()
    result = {k: raw[k] for k in BASE_IDENTITY_FIELDS if k in raw}
    control = raw.get("control_card")
    if isinstance(control, Mapping):
        result["control_card"] = {k: control[k] for k in CONTROL_IDENTITY_FIELDS if k in control}
    forbidden = {"secret", "token", "bearer", "credential", "refresh", "handle", "client_id", "client_metadata"}
    for path in leaf_paths:
        if (not isinstance(path, tuple) or len(path) < 2 or len(path) > 8
                or path[0] not in ("properties", "provenance")
                or any(not _text(k, 128) or any(word in k.lower() for word in forbidden) for k in path)):
            raise IssuerReadRefused("issuer_read_projection_config_invalid")
        value: Any = raw
        for key in path:
            value = value.get(key) if isinstance(value, Mapping) else None
        if value is None or type(value) not in (str, int, bool):
            continue
        node = result
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = value
    return result


def _snapshots(request: IssuerReadRequest, phase: str, snapshots: Any) -> list[dict[str, Any]]:
    if phase not in ("authorize", "validate") or not isinstance(snapshots, (tuple, list)):
        raise IssuerReadRefused("issuer_read_evidence_invalid")
    if phase == "authorize":
        if snapshots:
            raise IssuerReadRefused("issuer_read_evidence_invalid")
        return []
    if len(snapshots) != 2:
        raise IssuerReadRefused("issuer_read_pair_required")
    for target, row in zip(request.targets, snapshots):
        if (not isinstance(row, Mapping) or set(row) != {"target", "card_revision", "authority_fingerprint", "identity"}
                or row["target"] != target.to_dict() or type(row["card_revision"]) is not int
                or row["card_revision"] <= 0 or not _hash(row["authority_fingerprint"])
                or not isinstance(row["identity"], Mapping)):
            raise IssuerReadRefused("issuer_read_evidence_invalid")
        identity = row["identity"]
        if any(identity.get(k) != v for k, v in (("access_id", target.access_id),
                ("grantor_subject", target.owner_subject), ("issuer_kind", target.issuer_kind),
                ("issuer_ref", target.issuer_ref), ("card_revision", row["card_revision"]))):
            raise IssuerReadRefused("issuer_read_evidence_binding_invalid")
    # Normalize and detach mutable caller objects; no live mappings in a seal.
    return json.loads(json.dumps(snapshots, allow_nan=False))


def _payload(request: IssuerReadRequest, phase: str, snapshots: Any) -> dict[str, Any]:
    if not _request_valid(request):
        raise IssuerReadRefused("issuer_read_request_invalid")
    return {"request": request.to_dict(), "phase": phase, "snapshots": _snapshots(request, phase, snapshots)}


def _admission(bundle_id: str, operation: str, payload: Mapping[str, Any]) -> AdmissionRequest:
    return AdmissionRequest(resource=bundle_id, operation=operation,
        invocation_id=payload["request"]["request_id"], request_digest=read_digest(payload),
        approval_context={"protocol": ISSUER_READ_PROTOCOL})


def sign_issuer_read_request(*, secret: str | bytes, bundle_id: str, operation: str, service_id: str,
        request: IssuerReadRequest, phase: str = "authorize", snapshots: Any = (),
        now: int | None = None, nonce: str | None = None) -> dict[str, Any]:
    payload = _payload(request, phase, snapshots)
    timestamp, nonce = str(int(time.time()) if now is None else int(now)), nonce or secrets.token_urlsafe(24)
    admission = _admission(bundle_id, operation, payload)
    if admission.validation_error():
        raise IssuerReadRefused("issuer_read_transport_binding_invalid")
    signature = sign_admission_request(secret=secret, service_id=service_id, timestamp=timestamp, nonce=nonce,
        delegated_token=f"{ISSUER_READ_PROTOCOL}:{request.request_id}", request=admission)
    return {**payload, "service_proof": {"service_id": service_id, "timestamp": timestamp, "nonce": nonce, "signature": signature}}


def verify_issuer_read_request(*, secret: str | bytes, bundle_id: str, operation: str,
        expected_service_id: str, body: Mapping[str, Any], now: int | None = None) -> ServiceProofDecision:
    """Recipient must consume nonce atomically in its DURABLE shared store."""
    try:
        if not isinstance(body, Mapping) or set(body) != {"request", "phase", "snapshots", "service_proof"}:
            raise IssuerReadRefused("issuer_read_request_invalid")
        request = issuer_read_request_from_mapping(body["request"])
        payload = _payload(request, body["phase"], body["snapshots"])
        raw = body["service_proof"]
        if (not isinstance(raw, Mapping) or set(raw) != {"service_id", "timestamp", "nonce", "signature"}
                or not all(type(v) is str for v in raw.values())):
            raise IssuerReadRefused("service_proof_missing")
        proof = ServiceProof(**dict(raw))
        if not expected_service_id or proof.service_id != expected_service_id:
            raise IssuerReadRefused("service_id_invalid")
        return verify_admission_request(secret=secret, proof=proof,
            delegated_token=f"{ISSUER_READ_PROTOCOL}:{request.request_id}",
            request=_admission(bundle_id, operation, payload), now=now)
    except (IssuerReadRefused, TypeError, ValueError):
        return ServiceProofDecision(False, "issuer_read_request_invalid")


@dataclass(frozen=True)
class IssuerReadDecision:
    request: IssuerReadRequest
    issuer_kind: str
    phase: str
    snapshots_digest: str
    allowed: bool
    reason: str
    policy_version: str
    valid_until: datetime
    adapter_id: str
    _seal: object = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class _ReadSeal:
    registry: object
    adapter: object
    values: tuple[Any, ...]


def _values(decision: IssuerReadDecision) -> tuple[Any, ...]:
    return tuple(getattr(decision, k) for k in ("request", "issuer_kind", "phase", "snapshots_digest",
        "allowed", "reason", "policy_version", "valid_until", "adapter_id"))


class IssuerReadRegistry:
    """No owner-managed shortcut and no prepare/finalize/write capability."""
    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self._adapters: dict[str, Any] = {}
        self._identity = object()
        self._now = now or (lambda: datetime.now(timezone.utc))

    def register(self, adapter: Any) -> None:
        if not _text(adapter.issuer_kind) or not _text(adapter.adapter_id) or not callable(getattr(adapter, "decide_read", None)):
            raise ValueError("issuer_read_adapter_invalid")
        self._adapters[adapter.issuer_kind] = adapter

    def identity(self, authority: Any) -> dict[str, Any]:
        adapter = self._adapters.get(authority.issuer_kind)
        if adapter is None:
            raise IssuerReadRefused("issuer_read_adapter_unavailable")
        return project_identity(authority, getattr(adapter, "identity_leaf_paths", ()))

    def require(self, request: IssuerReadRequest, decision: Any, *, issuer_kind: str, phase: str, snapshots: Any = ()) -> None:
        adapter = self._adapters.get(issuer_kind)
        if (type(decision) is not IssuerReadDecision or type(decision._seal) is not _ReadSeal
                or decision._seal.registry is not self._identity or decision._seal.adapter is not adapter
                or decision._seal.values != _values(decision)):
            raise IssuerReadRefused("issuer_read_decision_unsealed")
        if (decision.request != request or decision.issuer_kind != issuer_kind or decision.phase != phase
                or decision.snapshots_digest != read_digest(_snapshots(request, phase, snapshots))):
            raise IssuerReadRefused("issuer_read_decision_binding_invalid")
        if not decision.allowed:
            raise IssuerReadRefused(decision.reason or "issuer_read_refused")
        now = self._now()
        if now.utcoffset() is None or decision.valid_until.utcoffset() is None or now >= decision.valid_until:
            raise IssuerReadRefused("issuer_read_decision_expired")

    async def decide(self, request: IssuerReadRequest, *, issuer_kind: str, phase: str = "authorize", snapshots: Any = ()) -> IssuerReadDecision:
        payload = _payload(request, phase, snapshots)
        if issuer_kind not in {t.issuer_kind for t in request.targets}:
            raise IssuerReadRefused("issuer_read_issuer_binding_invalid")
        adapter = self._adapters.get(issuer_kind)
        if adapter is None:
            raise IssuerReadRefused("issuer_read_adapter_unavailable")
        adapter_id = adapter.adapter_id
        try:
            allowed, reason, version, until = await adapter.decide_read(request, phase=phase, snapshots=payload["snapshots"])
            now = self._now()
            if (type(allowed) is not bool or type(reason) is not str or not _text(version)
                    or not isinstance(until, datetime) or until.utcoffset() is None or now.utcoffset() is None
                    or until <= now or until > now + timedelta(seconds=MAX_READ_SECONDS)):
                raise IssuerReadRefused("issuer_read_response_invalid")
            if self._adapters.get(issuer_kind) is not adapter or adapter.adapter_id != adapter_id:
                raise IssuerReadRefused("issuer_read_adapter_changed")
        except IssuerReadRefused:
            raise
        except Exception as exc:
            raise IssuerReadRefused("issuer_read_adapter_unavailable") from exc
        decision = IssuerReadDecision(request, issuer_kind, phase, read_digest(payload["snapshots"]),
                                      allowed, reason, version, until, adapter_id)
        return replace(decision, _seal=_ReadSeal(self._identity, adapter, _values(decision)))

    async def revalidate(self, request: IssuerReadRequest, decision: Any, *, snapshots: Any) -> IssuerReadDecision:
        if type(decision) is not IssuerReadDecision:
            raise IssuerReadRefused("issuer_read_decision_unsealed")
        self.require(request, decision, issuer_kind=decision.issuer_kind, phase="authorize")
        fresh = await self.decide(request, issuer_kind=decision.issuer_kind, phase="validate", snapshots=snapshots)
        if fresh.policy_version != decision.policy_version:
            raise IssuerReadRefused("issuer_read_policy_changed")
        fresh = replace(fresh, valid_until=min(decision.valid_until, fresh.valid_until))
        fresh = replace(fresh, _seal=_ReadSeal(self._identity, self._adapters[decision.issuer_kind], _values(fresh)))
        self.require(request, fresh, issuer_kind=fresh.issuer_kind, phase="validate", snapshots=snapshots)
        return fresh


class RemoteIssuerReadAdapter:
    def __init__(self, *, issuer_kind: str, adapter_id: str,
                 transport: Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]],
                 identity_leaf_paths: tuple[tuple[str, ...], ...] = (), timeout_seconds: float = 5.0) -> None:
        if not 0 < timeout_seconds <= 5:
            raise ValueError("issuer_read_transport_timeout_invalid")
        self.issuer_kind, self.adapter_id = issuer_kind, adapter_id
        self.identity_leaf_paths, self._transport, self._timeout = identity_leaf_paths, transport, timeout_seconds

    async def decide_read(self, request: IssuerReadRequest, *, phase: str, snapshots: Any) -> tuple[bool, str, str, datetime]:
        payload = _payload(request, phase, snapshots)
        response = await asyncio.wait_for(self._transport(payload), self._timeout)
        if (not isinstance(response, Mapping) or response.get("ok") is not True
                or issuer_read_request_from_mapping(response.get("request")) != request
                or response.get("phase") != phase or response.get("snapshots_digest") != read_digest(payload["snapshots"])):
            raise IssuerReadRefused("issuer_read_response_binding_invalid")
        decision = response.get("decision")
        if (not isinstance(decision, Mapping) or set(decision) != {"allowed", "reason", "policy_version", "valid_until"}
                or type(decision["allowed"]) is not bool
                or any(type(decision[k]) is not str for k in ("reason", "policy_version", "valid_until"))):
            raise IssuerReadRefused("issuer_read_response_invalid")
        return decision["allowed"], decision["reason"], decision["policy_version"], datetime.fromisoformat(decision["valid_until"].replace("Z", "+00:00"))
