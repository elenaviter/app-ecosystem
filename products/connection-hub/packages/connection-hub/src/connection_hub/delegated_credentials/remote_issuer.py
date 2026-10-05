"""Portable remote issuer adapter over a trusted, authenticated transport.

The hosting app supplies the transport and descriptor-selected peer identity.
No product-domain operation, endpoint, policy, or role is hard-coded here.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import asdict, fields
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping

from .admission import (
    AdmissionRequest, ServiceProof, ServiceProofDecision,
    sign_admission_request, verify_admission_request,
)
from .issuer_gate import IssuerRequest, _request_valid

ISSUER_PROTOCOL = "issuer-decision.v1"
MAX_ISSUER_TRANSPORT_SECONDS = 5.0
IssuerTransport = Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]]


def issuer_request_from_mapping(raw: object, *, allow_empty_context: bool = False) -> IssuerRequest:
    if not isinstance(raw, Mapping) or set(raw) != {f.name for f in fields(IssuerRequest)}:
        raise ValueError("issuer_request_invalid")
    request = IssuerRequest(**dict(raw))
    if not _request_valid(request, allow_empty_context=allow_empty_context):
        raise ValueError("issuer_request_invalid")
    return request


def _peer_request(bundle_id: str, operation: str, request: IssuerRequest) -> AdmissionRequest:
    return AdmissionRequest(
        resource=bundle_id,
        operation=operation,
        approval_context={
            "protocol": ISSUER_PROTOCOL,
            "request": json.dumps(asdict(request), sort_keys=True,
                                  separators=(",", ":"), ensure_ascii=True),
        },
    )


def sign_issuer_request(*, secret: str | bytes, bundle_id: str, operation: str,
                        service_id: str, request: IssuerRequest,
                        now: int | None = None, nonce: str | None = None) -> dict[str, Any]:
    if not _request_valid(request):
        raise ValueError("issuer_request_invalid")
    timestamp = str(int(time.time()) if now is None else int(now))
    nonce = nonce or secrets.token_urlsafe(24)
    signature = sign_admission_request(
        secret=secret, service_id=service_id, timestamp=timestamp, nonce=nonce,
        delegated_token=f"{ISSUER_PROTOCOL}:{request.request_id}",
        request=_peer_request(bundle_id, operation, request),
    )
    return {"request": asdict(request), "service_proof": {
        "service_id": service_id, "timestamp": timestamp,
        "nonce": nonce, "signature": signature,
    }}


def verify_issuer_request(*, secret: str | bytes, bundle_id: str, operation: str,
                          expected_service_id: str, body: Mapping[str, Any],
                          now: int | None = None) -> ServiceProofDecision:
    """Verify a peer body; the endpoint must separately claim its nonce once."""
    if not isinstance(body, Mapping) or set(body) != {"request", "service_proof"}:
        return ServiceProofDecision(False, "issuer_request_invalid")
    try:
        request = issuer_request_from_mapping(body["request"])
    except (TypeError, ValueError):
        return ServiceProofDecision(False, "issuer_request_invalid")
    raw = body["service_proof"]
    if (not isinstance(raw, Mapping)
            or set(raw) != {"service_id", "timestamp", "nonce", "signature"}
            or not all(type(v) is str for v in raw.values())):
        return ServiceProofDecision(False, "service_proof_missing")
    proof = ServiceProof(**dict(raw))
    if not expected_service_id or proof.service_id != expected_service_id:
        return ServiceProofDecision(False, "service_id_invalid")
    return verify_admission_request(
        secret=secret, proof=proof,
        delegated_token=f"{ISSUER_PROTOCOL}:{request.request_id}",
        request=_peer_request(bundle_id, operation, request), now=now,
    )


class RemoteIssuerAdapter:
    """Validate an authenticated peer answer; registry owns sealing/expiry cap."""

    def __init__(self, *, issuer_kind: str, adapter_id: str,
                 transport: IssuerTransport, timeout_seconds: float = 5.0,
                 prepare_transport: IssuerTransport | None = None,
                 finalize_transport: IssuerTransport | None = None) -> None:
        self.issuer_kind = issuer_kind
        self.adapter_id = adapter_id
        self._transport = transport
        self._prepare_transport = prepare_transport
        self._finalize_transport = finalize_transport
        if not 0 < timeout_seconds <= MAX_ISSUER_TRANSPORT_SECONDS:
            raise ValueError("issuer_transport_timeout_invalid")
        self._timeout = timeout_seconds

    async def prepare_context(self, request: IssuerRequest, *, current: Mapping[str, Any],
                              candidate: Mapping[str, Any]) -> str:
        if self._prepare_transport is None:
            raise ValueError("issuer_context_provider_unavailable")
        response = await asyncio.wait_for(self._prepare_transport({
            "request": asdict(request), "current": dict(current), "candidate": dict(candidate),
        }), self._timeout)
        if (not isinstance(response, Mapping) or response.get("ok") is not True
                or response.get("request") != asdict(request)
                or response.get("change_digest") != request.change_digest
                or type(response.get("context_ref")) is not str
                or not response["context_ref"].strip()):
            raise ValueError("issuer_context_response_invalid")
        return response["context_ref"]

    async def finalize_context(self, request: IssuerRequest, *, outcome: Mapping[str, Any]) -> bool:
        if self._finalize_transport is None:
            return False
        response = await asyncio.wait_for(self._finalize_transport({
            "request": asdict(request), "outcome": dict(outcome),
        }), self._timeout)
        return (isinstance(response, Mapping) and response.get("ok") is True
                and response.get("request") == asdict(request)
                and response.get("outcome") == dict(outcome)
                and response.get("finalized") is True)


    async def decide(self, request: IssuerRequest) -> tuple[bool, str, str, datetime]:
        # Only request data crosses the wire, never the in-process seal.
        response = await asyncio.wait_for(self._transport(asdict(request)), self._timeout)
        if not isinstance(response, Mapping) or response.get("ok") is not True:
            raise ValueError("issuer_provider_refused")
        echoed = issuer_request_from_mapping(response.get("request"))
        if echoed != request:
            raise ValueError("issuer_request_mismatch")
        decision = response.get("decision")
        if (not isinstance(decision, Mapping)
                or set(decision) != {"allowed", "reason", "policy_version", "valid_until"}
                or type(decision["allowed"]) is not bool
                or any(type(decision[k]) is not str for k in
                       ("reason", "policy_version", "valid_until"))):
            raise ValueError("issuer_response_invalid")
        until = datetime.fromisoformat(decision["valid_until"].replace("Z", "+00:00"))
        return (decision["allowed"], decision["reason"], decision["policy_version"], until)


def _envelope_request(bundle_id: str, operation: str, protocol: str,
                      payload: Mapping[str, Any]) -> AdmissionRequest:
    return AdmissionRequest(resource=bundle_id, operation=operation, approval_context={
        "protocol": protocol,
        "payload": json.dumps(payload, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=True, allow_nan=False),
    })


def sign_issuer_envelope(*, secret: str | bytes, bundle_id: str, operation: str,
                         service_id: str, protocol: str, payload: Mapping[str, Any],
                         now: int | None = None, nonce: str | None = None) -> dict[str, Any]:
    if protocol not in ("issuer-context.v1", "issuer-outcome.v1"):
        raise ValueError("issuer_protocol_invalid")
    request = issuer_request_from_mapping(payload.get("request"),
                                         allow_empty_context=protocol == "issuer-context.v1")
    timestamp = str(int(time.time()) if now is None else int(now))
    nonce = nonce or secrets.token_urlsafe(24)
    signature = sign_admission_request(
        secret=secret, service_id=service_id, timestamp=timestamp, nonce=nonce,
        delegated_token=f"{protocol}:{request.request_id}",
        request=_envelope_request(bundle_id, operation, protocol, payload),
    )
    return {**dict(payload), "service_proof": {
        "service_id": service_id, "timestamp": timestamp,
        "nonce": nonce, "signature": signature,
    }}


def verify_issuer_envelope(*, secret: str | bytes, bundle_id: str, operation: str,
                           expected_service_id: str, protocol: str,
                           body: Mapping[str, Any], now: int | None = None) -> ServiceProofDecision:
    if protocol not in ("issuer-context.v1", "issuer-outcome.v1") or not isinstance(body, Mapping):
        return ServiceProofDecision(False, "issuer_protocol_invalid")
    payload = {k: v for k, v in body.items() if k != "service_proof"}
    try:
        request = issuer_request_from_mapping(payload.get("request"),
                                             allow_empty_context=protocol == "issuer-context.v1")
        raw = body.get("service_proof")
        if (not isinstance(raw, Mapping)
                or set(raw) != {"service_id", "timestamp", "nonce", "signature"}
                or not all(type(v) is str for v in raw.values())):
            return ServiceProofDecision(False, "service_proof_missing")
        proof = ServiceProof(**dict(raw))
        if not expected_service_id or proof.service_id != expected_service_id:
            return ServiceProofDecision(False, "service_id_invalid")
        return verify_admission_request(
            secret=secret, proof=proof,
            delegated_token=f"{protocol}:{request.request_id}",
            request=_envelope_request(bundle_id, operation, protocol, payload), now=now,
        )
    except (TypeError, ValueError):
        return ServiceProofDecision(False, "issuer_request_invalid")
