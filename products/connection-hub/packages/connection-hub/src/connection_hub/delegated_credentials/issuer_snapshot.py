"""Protected full Card snapshots for an issuer, separate from the identity read.

The identity read (``issuer_read``) returns a redacted identity projection. An
issuer that must act on a Card's exact current authority, for example to
migrate grants with a compare-and-swap update, needs the complete original
revision instead. This module is that second, separately authorized read:

- its own protocol (``issuer-snapshot.v1``), proof, decision, seal and
  registry; nothing here widens or reinterprets an identity-read decision;
- the host binds the actual platform human and tenant/project; the public
  query carries only opaque coordinates;
- a fresh issuer decision authorizes the read, both production fences hold
  only for the raw committed reads (no peer I/O, cache, repair or receipt
  inside them), then a second fresh decision binds the complete snapshots and
  their full fingerprints, clamped to the first decision's expiry;
- both Cards or neither, as their original ``CardAuthority`` payloads and
  original ``content_hash``; credential material refuses the pair instead of
  being redacted, so a returned payload is always complete.

The payload carries personal data. It is returned only to the authenticated
caller and is never logged; refusals carry a reason code, never a payload.
Verification authenticates a wire proof only: the recipient MUST atomically
consume its nonce in a durable, shared store before policy evaluation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import urlsplit

from .admission import AdmissionRequest, ServiceProof, ServiceProofDecision, sign_admission_request, verify_admission_request
from .issuer_read import IssuerReadQuery, IssuerReadRefused, IssuerReadRequest, IssuerReadTarget, read_digest

_LOGGER = logging.getLogger(__name__)

ISSUER_SNAPSHOT_PROTOCOL = "issuer-snapshot.v1"
MAX_SNAPSHOT_SECONDS = 60
# Key names under the free-form fields that hold credential material, matched
# by how a key ENDS (``api_token``, ``client_secret``, ``credential_handle``).
# A substring match would refuse standard non-secret metadata such as OAuth's
# ``token_endpoint_auth_method``. Values are matched by shape, never by word,
# so a resource name such as ".../delegated_credentials/..." is not refused.
# A payload holding credential material is refused whole, never redacted.
CREDENTIAL_KEY_ENDINGS = ("token", "tokens", "secret", "secrets", "bearer", "handle", "handles", "password",
                          "passwords", "passwd", "credential", "credentials", "api_key", "apikey", "private_key",
                          "authorization", "cookie", "cookies")
# Free-form CardAuthority fields searched for credential material.
FREEFORM_FIELDS = ("properties", "provenance", "client_metadata", "named_services")
# Well-known issued-secret prefixes (GitHub, Slack, OpenAI-style, AWS key id).
TOKEN_PREFIXES = ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_", "xoxb-", "xoxp-", "xoxa-", "sk-", "akia")
_B64URL = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


class IssuerSnapshotRefused(RuntimeError):
    def __init__(self, reason: str, *, retryable: bool = False) -> None:
        super().__init__(reason)
        self.reason, self.retryable = reason, retryable


def _text(value: Any, limit: int = 1024) -> bool:
    return type(value) is str and bool(value.strip()) and len(value.encode("utf-8")) <= limit


def _hash(value: Any) -> bool:
    return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


@dataclass(frozen=True)
class IssuerSnapshotRequest:
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
    def snapshot_digest(self) -> str:
        return read_digest({"protocol": ISSUER_SNAPSHOT_PROTOCOL, "request": self.payload()})

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload(), "snapshot_digest": self.snapshot_digest}

    def storage_request(self) -> IssuerReadRequest:
        """The mechanical coordinate DTO the fenced storage read takes; never a decision or seal."""
        return IssuerReadRequest(self.actor_subject, self.actor_classification, self.tenant, self.project,
                                 self.context_ref, self.request_id, self.targets)


def _query(raw: Any) -> IssuerReadQuery:
    try:
        return IssuerReadQuery.from_mapping(raw)
    except IssuerReadRefused as exc:
        raise IssuerSnapshotRefused(exc.reason.replace("issuer_read_", "issuer_snapshot_", 1)) from None


def issuer_snapshot_request_from_mapping(raw: Any) -> IssuerSnapshotRequest:
    keys = {"actor_subject", "actor_classification", "tenant", "project", "context_ref", "request_id",
            "targets", "snapshot_digest"}
    if not isinstance(raw, Mapping) or set(raw) != keys:
        raise IssuerSnapshotRefused("issuer_snapshot_request_invalid")
    query = _query({k: raw[k] for k in ("context_ref", "request_id", "targets")})
    if (not all(_text(raw[k]) for k in ("actor_subject", "tenant", "project"))
            or raw["actor_classification"] not in ("registered", "privileged")
            or raw["actor_subject"] == "anonymous"
            or raw["actor_subject"].startswith(("integration:", "telegram_"))):
        raise IssuerSnapshotRefused("issuer_snapshot_requires_platform_human")
    request = IssuerSnapshotRequest(raw["actor_subject"], raw["actor_classification"], raw["tenant"], raw["project"],
                                    query.context_ref, query.request_id, query.targets)
    if raw["targets"] != request.to_dict()["targets"] or raw["snapshot_digest"] != request.snapshot_digest:
        raise IssuerSnapshotRefused("issuer_snapshot_request_binding_invalid")
    return request


def _request_valid(request: Any) -> bool:
    try:
        return (type(request) is IssuerSnapshotRequest
                and issuer_snapshot_request_from_mapping(request.to_dict()) == request)
    except (IssuerSnapshotRefused, TypeError, ValueError, AttributeError):
        return False


def _secret_shaped(value: str) -> bool:
    """A value that looks like a credential by its shape, never by a word in it."""
    text = value.strip()
    lowered = text.lower()
    if lowered.startswith(("bearer ", "basic ")) or lowered.startswith(TOKEN_PREFIXES):
        return True
    parts = text.split(".")
    if len(parts) == 3 and all(len(part) >= 8 and set(part) <= _B64URL for part in parts):
        return True  # a JWT: three dot-separated base64url parts
    # A long unbroken high-entropy run: mixed case and digits, no separators.
    # Hex digests are lowercase only, so a content hash is not caught here.
    return (len(text) >= 32 and set(text) <= _B64URL | {"+", "/", "="} and "/" not in text.rstrip("=")
            and any(c.isdigit() for c in text) and any(c.isupper() for c in text) and any(c.islower() for c in text))


def _credential_material(value: Any, depth: int = 0) -> bool:
    if depth > 16:
        return True  # unbounded nesting is not reviewable; refuse
    if isinstance(value, Mapping):
        return any((type(key) is str and key.lower().endswith(CREDENTIAL_KEY_ENDINGS))
                   or _credential_material(item, depth + 1) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_credential_material(item, depth + 1) for item in value)
    return type(value) is str and _secret_shaped(value)


def _unsafe_manage_url(url: Any) -> bool:
    if not url:
        return False
    if type(url) is not str:
        return True
    parts = urlsplit(url)
    return (parts.scheme != "https" or not parts.hostname or parts.username is not None
            or parts.password is not None or bool(parts.query) or bool(parts.fragment))


def full_authority(authority: Any) -> dict[str, Any]:
    """The complete original payload, or a refusal; never a redacted copy.

    ``last_four`` is display metadata of at most four characters and is
    returned, because dropping it would break the original content hash; a
    longer value may be a whole token in the wrong field and refuses. A manage
    URL refuses only when unsafe (not https, userinfo, query or fragment). A
    credential-named key or a secret-shaped value in a free-form field refuses.
    """
    raw = authority.to_dict()
    last_four = raw.get("last_four") or ""
    if (type(last_four) is not str or len(last_four) > 4 or _unsafe_manage_url(raw.get("manage_url"))
            or any(_credential_material(raw.get(name)) for name in FREEFORM_FIELDS)):
        raise IssuerSnapshotRefused("issuer_snapshot_credential_material")
    return raw


def _snapshots(request: IssuerSnapshotRequest, phase: str, snapshots: Any) -> list[dict[str, Any]]:
    if phase not in ("authorize", "validate") or not isinstance(snapshots, (tuple, list)):
        raise IssuerSnapshotRefused("issuer_snapshot_evidence_invalid")
    if phase == "authorize":
        if snapshots:
            raise IssuerSnapshotRefused("issuer_snapshot_evidence_invalid")
        return []
    if len(snapshots) != 2:
        raise IssuerSnapshotRefused("issuer_snapshot_pair_required")
    for target, row in zip(request.targets, snapshots):
        if (not isinstance(row, Mapping) or set(row) != {"target", "card_revision", "authority_fingerprint", "authority"}
                or row["target"] != target.to_dict() or type(row["card_revision"]) is not int
                or row["card_revision"] <= 0 or not _hash(row["authority_fingerprint"])
                or not isinstance(row["authority"], Mapping)):
            raise IssuerSnapshotRefused("issuer_snapshot_evidence_invalid")
        authority = row["authority"]
        if any(authority.get(k) != v for k, v in (("access_id", target.access_id),
                ("grantor_subject", target.owner_subject), ("issuer_kind", target.issuer_kind),
                ("issuer_ref", target.issuer_ref), ("card_revision", row["card_revision"]))):
            raise IssuerSnapshotRefused("issuer_snapshot_evidence_binding_invalid")
    # Normalize and detach caller objects; no live mappings in a seal.
    return json.loads(json.dumps(snapshots, allow_nan=False))


def _payload(request: IssuerSnapshotRequest, phase: str, snapshots: Any) -> dict[str, Any]:
    if not _request_valid(request):
        raise IssuerSnapshotRefused("issuer_snapshot_request_invalid")
    return {"request": request.to_dict(), "phase": phase, "snapshots": _snapshots(request, phase, snapshots)}


def _admission(bundle_id: str, operation: str, payload: Mapping[str, Any]) -> AdmissionRequest:
    return AdmissionRequest(resource=bundle_id, operation=operation,
        invocation_id=payload["request"]["request_id"], request_digest=read_digest(payload),
        approval_context={"protocol": ISSUER_SNAPSHOT_PROTOCOL})


def sign_issuer_snapshot_request(*, secret: str | bytes, bundle_id: str, operation: str, service_id: str,
        request: IssuerSnapshotRequest, phase: str = "authorize", snapshots: Any = (),
        now: int | None = None, nonce: str | None = None) -> dict[str, Any]:
    payload = _payload(request, phase, snapshots)
    timestamp, nonce = str(int(time.time()) if now is None else int(now)), nonce or secrets.token_urlsafe(24)
    admission = _admission(bundle_id, operation, payload)
    if admission.validation_error():
        raise IssuerSnapshotRefused("issuer_snapshot_transport_binding_invalid")
    signature = sign_admission_request(secret=secret, service_id=service_id, timestamp=timestamp, nonce=nonce,
        delegated_token=f"{ISSUER_SNAPSHOT_PROTOCOL}:{request.request_id}", request=admission)
    return {**payload, "service_proof": {"service_id": service_id, "timestamp": timestamp, "nonce": nonce, "signature": signature}}


def verify_issuer_snapshot_request(*, secret: str | bytes, bundle_id: str, operation: str,
        expected_service_id: str, body: Mapping[str, Any], now: int | None = None) -> ServiceProofDecision:
    """Recipient must consume nonce atomically in its DURABLE shared store."""
    try:
        if not isinstance(body, Mapping) or set(body) != {"request", "phase", "snapshots", "service_proof"}:
            raise IssuerSnapshotRefused("issuer_snapshot_request_invalid")
        request = issuer_snapshot_request_from_mapping(body["request"])
        payload = _payload(request, body["phase"], body["snapshots"])
        raw = body["service_proof"]
        if (not isinstance(raw, Mapping) or set(raw) != {"service_id", "timestamp", "nonce", "signature"}
                or not all(type(v) is str for v in raw.values())):
            raise IssuerSnapshotRefused("service_proof_missing")
        proof = ServiceProof(**dict(raw))
        if not expected_service_id or proof.service_id != expected_service_id:
            raise IssuerSnapshotRefused("service_id_invalid")
        return verify_admission_request(secret=secret, proof=proof,
            delegated_token=f"{ISSUER_SNAPSHOT_PROTOCOL}:{request.request_id}",
            request=_admission(bundle_id, operation, payload), now=now)
    except (IssuerSnapshotRefused, TypeError, ValueError):
        return ServiceProofDecision(False, "issuer_snapshot_request_invalid")


@dataclass(frozen=True)
class IssuerSnapshotDecision:
    request: IssuerSnapshotRequest
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
class _SnapshotSeal:
    registry: object
    adapter: object
    values: tuple[Any, ...]


def _values(decision: IssuerSnapshotDecision) -> tuple[Any, ...]:
    return tuple(getattr(decision, k) for k in ("request", "issuer_kind", "phase", "snapshots_digest",
        "allowed", "reason", "policy_version", "valid_until", "adapter_id"))


class IssuerSnapshotRegistry:
    """Full-snapshot decisions only; no identity-read, prepare/finalize or write capability."""

    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self._adapters: dict[str, Any] = {}
        self._identity = object()
        self._now = now or (lambda: datetime.now(timezone.utc))

    def register(self, adapter: Any) -> None:
        if (not _text(adapter.issuer_kind) or not _text(adapter.adapter_id)
                or not callable(getattr(adapter, "decide_snapshot", None))):
            raise ValueError("issuer_snapshot_adapter_invalid")
        self._adapters[adapter.issuer_kind] = adapter

    def require(self, request: IssuerSnapshotRequest, decision: Any, *, issuer_kind: str, phase: str,
                snapshots: Any = ()) -> None:
        adapter = self._adapters.get(issuer_kind)
        if (type(decision) is not IssuerSnapshotDecision or type(decision._seal) is not _SnapshotSeal
                or decision._seal.registry is not self._identity or decision._seal.adapter is not adapter
                or decision._seal.values != _values(decision)):
            raise IssuerSnapshotRefused("issuer_snapshot_decision_unsealed")
        if (decision.request != request or decision.issuer_kind != issuer_kind or decision.phase != phase
                or decision.snapshots_digest != read_digest(_snapshots(request, phase, snapshots))):
            raise IssuerSnapshotRefused("issuer_snapshot_decision_binding_invalid")
        if not decision.allowed:
            raise IssuerSnapshotRefused(decision.reason or "issuer_snapshot_refused")
        now = self._now()
        if now.utcoffset() is None or decision.valid_until.utcoffset() is None or now >= decision.valid_until:
            raise IssuerSnapshotRefused("issuer_snapshot_decision_expired")

    async def decide(self, request: IssuerSnapshotRequest, *, issuer_kind: str, phase: str = "authorize",
                     snapshots: Any = ()) -> IssuerSnapshotDecision:
        payload = _payload(request, phase, snapshots)
        if issuer_kind not in {t.issuer_kind for t in request.targets}:
            raise IssuerSnapshotRefused("issuer_snapshot_issuer_binding_invalid")
        adapter = self._adapters.get(issuer_kind)
        if adapter is None:
            raise IssuerSnapshotRefused("issuer_snapshot_adapter_unavailable")
        adapter_id = adapter.adapter_id
        try:
            allowed, reason, version, until = await adapter.decide_snapshot(request, phase=phase,
                                                                            snapshots=payload["snapshots"])
            now = self._now()
            if (type(allowed) is not bool or type(reason) is not str or not _text(version)
                    or not isinstance(until, datetime) or until.utcoffset() is None or now.utcoffset() is None
                    or until <= now or until > now + timedelta(seconds=MAX_SNAPSHOT_SECONDS)):
                raise IssuerSnapshotRefused("issuer_snapshot_response_invalid")
            if self._adapters.get(issuer_kind) is not adapter or adapter.adapter_id != adapter_id:
                raise IssuerSnapshotRefused("issuer_snapshot_adapter_changed")
        except IssuerSnapshotRefused:
            raise
        except Exception as exc:
            raise IssuerSnapshotRefused("issuer_snapshot_adapter_unavailable") from exc
        decision = IssuerSnapshotDecision(request, issuer_kind, phase, read_digest(payload["snapshots"]),
                                          allowed, reason, version, until, adapter_id)
        return replace(decision, _seal=_SnapshotSeal(self._identity, adapter, _values(decision)))

    async def revalidate(self, request: IssuerSnapshotRequest, decision: Any, *, snapshots: Any) -> IssuerSnapshotDecision:
        if type(decision) is not IssuerSnapshotDecision:
            raise IssuerSnapshotRefused("issuer_snapshot_decision_unsealed")
        self.require(request, decision, issuer_kind=decision.issuer_kind, phase="authorize")
        fresh = await self.decide(request, issuer_kind=decision.issuer_kind, phase="validate", snapshots=snapshots)
        if fresh.policy_version != decision.policy_version:
            raise IssuerSnapshotRefused("issuer_snapshot_policy_changed")
        fresh = replace(fresh, valid_until=min(decision.valid_until, fresh.valid_until))
        fresh = replace(fresh, _seal=_SnapshotSeal(self._identity, self._adapters[decision.issuer_kind], _values(fresh)))
        self.require(request, fresh, issuer_kind=fresh.issuer_kind, phase="validate", snapshots=snapshots)
        return fresh


class RemoteIssuerSnapshotAdapter:
    def __init__(self, *, issuer_kind: str, adapter_id: str,
                 transport: Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]],
                 timeout_seconds: float = 5.0) -> None:
        if not 0 < timeout_seconds <= 5:
            raise ValueError("issuer_snapshot_transport_timeout_invalid")
        self.issuer_kind, self.adapter_id = issuer_kind, adapter_id
        self._transport, self._timeout = transport, timeout_seconds

    async def decide_snapshot(self, request: IssuerSnapshotRequest, *, phase: str,
                              snapshots: Any) -> tuple[bool, str, str, datetime]:
        payload = _payload(request, phase, snapshots)
        response = await asyncio.wait_for(self._transport(payload), self._timeout)
        if (not isinstance(response, Mapping) or response.get("ok") is not True
                or issuer_snapshot_request_from_mapping(response.get("request")) != request
                or response.get("phase") != phase
                or response.get("snapshots_digest") != read_digest(payload["snapshots"])):
            raise IssuerSnapshotRefused("issuer_snapshot_response_binding_invalid")
        decision = response.get("decision")
        if (not isinstance(decision, Mapping) or set(decision) != {"allowed", "reason", "policy_version", "valid_until"}
                or type(decision["allowed"]) is not bool
                or any(type(decision[k]) is not str for k in ("reason", "policy_version", "valid_until"))):
            raise IssuerSnapshotRefused("issuer_snapshot_response_invalid")
        return (decision["allowed"], decision["reason"], decision["policy_version"],
                datetime.fromisoformat(decision["valid_until"].replace("Z", "+00:00")))


async def issuer_managed_card_snapshots(query: Mapping[str, Any], *, actor_subject: str, actor_classification: str,
        tenant: str, project: str, registry: IssuerSnapshotRegistry | None, persistence: Any) -> dict[str, Any]:
    """Both complete Card snapshots or neither; no reservation, repair or mutation effects.

    The actor and tenant/project are the host's bound values, never read from
    the query. The storage read runs between the two decisions with no peer
    I/O inside its fences.
    """
    try:
        q = _query(query)
        if registry is None:
            raise IssuerSnapshotRefused("issuer_snapshot_host_unavailable")
        request = issuer_snapshot_request_from_mapping(IssuerSnapshotRequest(
            actor_subject, actor_classification, tenant, project, q.context_ref, q.request_id, q.targets).to_dict())
        reader = getattr(persistence, "read_lifecycle_identities", None)
        if not callable(reader):
            raise IssuerSnapshotRefused("issuer_snapshot_port_unavailable")
        async with asyncio.timeout(30):
            first = []
            for kind in dict.fromkeys(t.issuer_kind for t in request.targets):
                decision = await registry.decide(request, issuer_kind=kind)
                registry.require(request, decision, issuer_kind=kind, phase="authorize")
                first.append(decision)
            # No peer call within the storage port's two ordered Card fences.
            try:
                authorities = await reader(request.storage_request())
            except IssuerReadRefused as exc:
                raise IssuerSnapshotRefused(exc.reason.replace("issuer_read_", "issuer_snapshot_", 1),
                                            retryable=exc.retryable) from None
            if not isinstance(authorities, (tuple, list)) or len(authorities) != 2:
                raise IssuerSnapshotRefused("issuer_snapshot_pair_required")
            snapshots = []
            for target, authority in zip(request.targets, authorities):
                if (authority.access_id, authority.grantor_subject, authority.issuer_kind, authority.issuer_ref) != (
                        target.access_id, target.owner_subject, target.issuer_kind, target.issuer_ref):
                    raise IssuerSnapshotRefused("issuer_snapshot_target_binding_invalid")
                payload = full_authority(authority)
                snapshots.append({"target": target.to_dict(), "card_revision": authority.card_revision,
                                  "authority_fingerprint": authority.content_hash(), "authority": payload})
            fresh = [await registry.revalidate(request, decision, snapshots=snapshots) for decision in first]
            # Earlier decisions must still be live after the last peer await.
            for decision in fresh:
                registry.require(request, decision, issuer_kind=decision.issuer_kind, phase="validate",
                                 snapshots=snapshots)
            return {"ok": True, "status": 200, "request": request.to_dict(),
                    "snapshot_digest": request.snapshot_digest, "snapshots": snapshots}
    except IssuerSnapshotRefused as exc:
        return {"ok": False, "status": 409 if exc.retryable else 403, "error": exc.reason, "retryable": exc.retryable}
    except TimeoutError:
        return {"ok": False, "status": 503, "error": "issuer_snapshot_timeout", "retryable": True}
    except Exception:
        # Never log or return authorities, personal data or transport details.
        _LOGGER.warning("[connection-hub] protected full snapshot unavailable")
        return {"ok": False, "status": 503, "error": "issuer_snapshot_unavailable", "retryable": True}
