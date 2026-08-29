# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Direct protected-service admission contracts.

The caller's delegated bearer and the protected service's workload proof are
independent. The host verifies and resolves both, then passes current card and
catalog facts through the same managed-surface policy used by hosted doors.
This module owns the transport-neutral pieces: service registration parsing,
signed-request verification, resource binding, account narrowing, and bounded
principal projection.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from prokura.delegated_credentials.credential_view import (
    DelegatedCredentialView,
    normalize_resource,
    resource_matches,
)

ADMISSION_SCHEMA = "prokura.delegated_admission.v1"
ADMISSION_SIGNATURE_VERSION = "prokura-admission-v1"

SERVICE_ID_HEADER = "x-prokura-service-id"
SERVICE_TIMESTAMP_HEADER = "x-prokura-timestamp"
SERVICE_NONCE_HEADER = "x-prokura-nonce"
SERVICE_SIGNATURE_HEADER = "x-prokura-signature"

DEFAULT_MAX_CLOCK_SKEW_SECONDS = 300
DEFAULT_NONCE_TTL_SECONDS = 600
MIN_SERVICE_SECRET_BYTES = 32

_SERVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,127}$")
_NONCE_RE = re.compile(r"^[A-Za-z0-9._~-]{16,256}$")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return _text(value).lower() in {"1", "true", "yes", "on", "enabled"}


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        values: Iterable[Any] = value.replace(",", " ").split()
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = ()
    out: list[str] = []
    for item in values:
        text = _text(item)
        if text and text not in out:
            out.append(text)
    return tuple(out)


@dataclass(frozen=True)
class ProtectedService:
    """One registered policy-enforcement workload.

    ``resources`` references capability rows in the active delegated catalog;
    it does not redefine operations or grants.
    """

    service_id: str
    secret_ref: str
    resources: tuple[str, ...]
    label: str = ""
    enabled: bool = True

    def allows_resource(self, resource: str) -> bool:
        requested = normalize_resource(resource)
        return bool(requested) and any(
            resource_matches(selector, requested) for selector in self.resources
        )


@dataclass(frozen=True)
class AdmissionConfig:
    enabled: bool = False
    identity_projection_secret_ref: str = ""
    max_clock_skew_seconds: int = DEFAULT_MAX_CLOCK_SKEW_SECONDS
    nonce_ttl_seconds: int = DEFAULT_NONCE_TTL_SECONDS
    services: Mapping[str, ProtectedService] = field(default_factory=dict)

    @classmethod
    def from_connections(cls, connections: Mapping[str, Any] | None) -> "AdmissionConfig":
        delegated = (connections or {}).get("delegated_credentials")
        delegated = delegated if isinstance(delegated, Mapping) else {}
        raw = delegated.get("admission")
        node = raw if isinstance(raw, Mapping) else {}
        raw_services = node.get("services")
        rows: list[Mapping[str, Any]] = []
        if isinstance(raw_services, Mapping):
            for service_id, value in raw_services.items():
                row = dict(value) if isinstance(value, Mapping) else {}
                row.setdefault("service_id", service_id)
                rows.append(row)
        elif isinstance(raw_services, (list, tuple)):
            rows = [row for row in raw_services if isinstance(row, Mapping)]

        services: dict[str, ProtectedService] = {}
        for row in rows:
            service_id = _text(row.get("service_id") or row.get("id"))
            secret_ref = _text(row.get("secret_ref"))
            resources = _strings(
                row.get("resources")
                or row.get("resource_selectors")
                or row.get("resource")
            )
            if not service_id or not _SERVICE_ID_RE.fullmatch(service_id):
                continue
            services[service_id] = ProtectedService(
                service_id=service_id,
                secret_ref=secret_ref,
                resources=resources,
                label=_text(row.get("label")),
                enabled=_bool(row.get("enabled"), True),
            )

        skew = _positive_int(
            node.get("max_clock_skew_seconds"), DEFAULT_MAX_CLOCK_SKEW_SECONDS
        )
        nonce_ttl = max(
            skew * 2,
            _positive_int(node.get("nonce_ttl_seconds"), DEFAULT_NONCE_TTL_SECONDS),
        )
        return cls(
            enabled=_bool(node.get("enabled"), False),
            identity_projection_secret_ref=_text(
                node.get("identity_projection_secret_ref")
            ),
            max_clock_skew_seconds=skew,
            nonce_ttl_seconds=nonce_ttl,
            services=services,
        )

    def service(self, service_id: str) -> ProtectedService | None:
        service = self.services.get(_text(service_id))
        return service if service is not None and service.enabled else None


@dataclass(frozen=True)
class AdmissionAccount:
    provider_id: str = ""
    account_id: str = ""
    claims: tuple[str, ...] = ()

    @property
    def present(self) -> bool:
        return bool(self.provider_id or self.account_id or self.claims)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.provider_id:
            out["provider_id"] = self.provider_id
        if self.account_id:
            out["account_id"] = self.account_id
        if self.claims:
            out["claims"] = list(self.claims)
        return out


@dataclass(frozen=True)
class AdmissionRequest:
    resource: str
    operation: str
    account: AdmissionAccount = field(default_factory=AdmissionAccount)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "AdmissionRequest":
        node = value if isinstance(value, Mapping) else {}
        raw_account = node.get("account")
        account_node = raw_account if isinstance(raw_account, Mapping) else {}
        claims = tuple(sorted(_strings(account_node.get("claims"))))
        return cls(
            resource=normalize_resource(node.get("resource")),
            operation=_text(node.get("operation")),
            account=AdmissionAccount(
                provider_id=_text(
                    account_node.get("provider_id") or account_node.get("provider")
                ),
                account_id=_text(account_node.get("account_id")),
                claims=claims,
            ),
        )

    def validation_error(self) -> str:
        if not self.resource:
            return "resource_missing"
        if not self.operation:
            return "operation_missing"
        if self.account.present and not (
            self.account.provider_id and self.account.account_id
        ):
            return "account_incomplete"
        return ""

    def signing_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "operation": self.operation,
            "resource": self.resource,
        }
        if self.account.present:
            out["account"] = self.account.to_dict()
        return out


@dataclass(frozen=True)
class ServiceProof:
    service_id: str
    timestamp: str
    nonce: str
    signature: str

    @classmethod
    def from_headers(cls, headers: Mapping[str, Any] | None) -> "ServiceProof":
        source = headers if isinstance(headers, Mapping) else {}
        normalized = {str(key).strip().lower(): value for key, value in source.items()}
        return cls(
            service_id=_text(normalized.get(SERVICE_ID_HEADER)),
            timestamp=_text(normalized.get(SERVICE_TIMESTAMP_HEADER)),
            nonce=_text(normalized.get(SERVICE_NONCE_HEADER)),
            signature=_text(normalized.get(SERVICE_SIGNATURE_HEADER)),
        )


@dataclass(frozen=True)
class ServiceProofDecision:
    allowed: bool
    reason: str = ""


@dataclass(frozen=True)
class AccountScopeDecision:
    allowed: bool
    reason: str = ""
    claims: tuple[str, ...] = ()


def canonical_admission_message(
    *,
    service_id: str,
    timestamp: str,
    nonce: str,
    delegated_token: str,
    request: AdmissionRequest,
) -> bytes:
    body = json.dumps(
        request.signing_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    body_hash = hashlib.sha256(body).hexdigest()
    token_hash = hashlib.sha256(delegated_token.encode("utf-8")).hexdigest()
    return (
        f"{ADMISSION_SIGNATURE_VERSION}\n{service_id}\n{timestamp}\n{nonce}\n"
        f"{token_hash}\n{body_hash}"
    ).encode("utf-8")


def sign_admission_request(
    *,
    secret: str | bytes,
    service_id: str,
    timestamp: str,
    nonce: str,
    delegated_token: str,
    request: AdmissionRequest,
) -> str:
    secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
    digest = hmac.new(
        secret_bytes,
        canonical_admission_message(
            service_id=service_id,
            timestamp=timestamp,
            nonce=nonce,
            delegated_token=delegated_token,
            request=request,
        ),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify_admission_request(
    *,
    secret: str | bytes,
    proof: ServiceProof,
    delegated_token: str,
    request: AdmissionRequest,
    max_clock_skew_seconds: int = DEFAULT_MAX_CLOCK_SKEW_SECONDS,
    now: int | None = None,
) -> ServiceProofDecision:
    secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
    if len(secret_bytes) < MIN_SERVICE_SECRET_BYTES:
        return ServiceProofDecision(False, "service_secret_too_short")
    if not _SERVICE_ID_RE.fullmatch(_text(proof.service_id)):
        return ServiceProofDecision(False, "service_id_invalid")
    if not _NONCE_RE.fullmatch(_text(proof.nonce)):
        return ServiceProofDecision(False, "nonce_invalid")
    try:
        issued_at = int(proof.timestamp)
    except (TypeError, ValueError):
        return ServiceProofDecision(False, "timestamp_invalid")
    current = int(time.time()) if now is None else int(now)
    if abs(current - issued_at) > max(1, int(max_clock_skew_seconds)):
        return ServiceProofDecision(False, "timestamp_outside_window")
    if not delegated_token:
        return ServiceProofDecision(False, "delegated_token_missing")
    if request.validation_error():
        return ServiceProofDecision(False, request.validation_error())
    expected = sign_admission_request(
        secret=secret_bytes,
        service_id=proof.service_id,
        timestamp=proof.timestamp,
        nonce=proof.nonce,
        delegated_token=delegated_token,
        request=request,
    )
    if not hmac.compare_digest(expected, _text(proof.signature)):
        return ServiceProofDecision(False, "signature_invalid")
    return ServiceProofDecision(True)


def authorize_account_scope(
    view: DelegatedCredentialView,
    account: AdmissionAccount,
) -> AccountScopeDecision:
    if not account.present:
        return AccountScopeDecision(True)
    if not account.provider_id or not account.account_id:
        return AccountScopeDecision(False, "account_incomplete")
    provider_scope = view.account_claim_scope(account.provider_id)
    if provider_scope is None:
        return AccountScopeDecision(False, "delegated_credential_missing")

    held: set[str] = set()
    for selector in (account.account_id, "*"):
        held.update(provider_scope.get(selector, ()))
    if not held:
        return AccountScopeDecision(False, "account_not_granted")
    requested = set(account.claims)
    if requested and "*" not in held and not held.issuperset(requested):
        return AccountScopeDecision(False, "account_claim_not_granted")
    effective = requested if requested else held
    return AccountScopeDecision(True, claims=tuple(sorted(effective)))


def pairwise_service_subject(
    *,
    secret: str | bytes,
    service_id: str,
    grantor_user_id: str,
) -> str:
    secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
    if len(secret_bytes) < MIN_SERVICE_SECRET_BYTES:
        raise ValueError("identity projection secret must contain at least 32 bytes")
    message = f"prokura-service-subject-v1\n{service_id}\n{grantor_user_id}".encode(
        "utf-8"
    )
    digest = hmac.new(secret_bytes, message, hashlib.sha256).digest()
    encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return f"prk_sub_{encoded}"


def admission_denial(
    *,
    code: str,
    message: str,
    retryable: bool = False,
    decision_id: str = "",
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ret: dict[str, Any] = {"reason": _text(code)}
    if decision_id:
        ret["decision_id"] = decision_id
    if isinstance(details, Mapping) and details:
        ret["details"] = dict(details)
    return {
        "ok": False,
        "allowed": False,
        "schema": ADMISSION_SCHEMA,
        "error": {
            "code": _text(code) or "admission_denied",
            "message": _text(message) or "The delegated operation was denied.",
            "where": "prokura.delegated_admission",
            "retryable": bool(retryable),
        },
        "ret": ret,
    }


def admission_allow(
    *,
    decision_id: str,
    service_id: str,
    subject: str,
    view: DelegatedCredentialView,
    request: AdmissionRequest,
    available_grants: Iterable[str],
    account_claims: Iterable[str] = (),
    expires_at: int = 0,
    active_catalog_version: str = "",
) -> dict[str, Any]:
    authority: dict[str, Any] = {
        "resource": request.resource,
        "operation": request.operation,
        "grants": sorted({_text(item) for item in available_grants if _text(item)}),
    }
    if request.account.present:
        authority["account_scope"] = {
            "provider_id": request.account.provider_id,
            "account_id": request.account.account_id,
            "claims": sorted({_text(item) for item in account_claims if _text(item)}),
        }
    return {
        "ok": True,
        "allowed": True,
        "schema": ADMISSION_SCHEMA,
        "decision_id": decision_id,
        "service_id": service_id,
        "principal": {
            "sub": subject,
            "client_id": view.client_id,
        },
        "authority": authority,
        "provenance": {
            "card_revision": view.card_revision,
            "card_catalog_version": view.catalog_version,
            "active_catalog_version": _text(active_catalog_version),
        },
        "expires_at": max(0, int(expires_at or 0)),
    }


__all__ = [
    "ADMISSION_SCHEMA",
    "ADMISSION_SIGNATURE_VERSION",
    "DEFAULT_MAX_CLOCK_SKEW_SECONDS",
    "DEFAULT_NONCE_TTL_SECONDS",
    "MIN_SERVICE_SECRET_BYTES",
    "SERVICE_ID_HEADER",
    "SERVICE_NONCE_HEADER",
    "SERVICE_SIGNATURE_HEADER",
    "SERVICE_TIMESTAMP_HEADER",
    "AccountScopeDecision",
    "AdmissionAccount",
    "AdmissionConfig",
    "AdmissionRequest",
    "ProtectedService",
    "ServiceProof",
    "ServiceProofDecision",
    "admission_allow",
    "admission_denial",
    "authorize_account_scope",
    "canonical_admission_message",
    "pairwise_service_subject",
    "sign_admission_request",
    "verify_admission_request",
]
