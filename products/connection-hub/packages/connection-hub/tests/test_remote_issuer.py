from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import copy
import hashlib
import json

import pytest

from connection_hub.delegated_credentials.issuer_gate import IssuerRegistry
from connection_hub.delegated_credentials.admission import (
    AdmissionRequest, ServiceProof, sign_admission_request, verify_admission_request,
)
from connection_hub.delegated_credentials.remote_issuer import (
    RemoteIssuerAdapter, issuer_request_from_mapping, sign_issuer_request,
    verify_issuer_request,
    sign_issuer_envelope, verify_issuer_envelope,
    issuer_payload_digest,
)
from test_issuer_gate import REQUEST
from test_project_control_cards import _regular_control

SECRET = "x" * 32
BUNDLE = "authority@1-0"
OPERATION = "opaque_issuer_decide"
SERVICE = "configured-hub"


def signed(**kwargs):
    return sign_issuer_request(secret=SECRET, bundle_id=BUNDLE, operation=OPERATION,
                               service_id=SERVICE, request=REQUEST, now=1000, **kwargs)


def verify(body, **kwargs):
    defaults = dict(secret=SECRET, bundle_id=BUNDLE, operation=OPERATION,
                    expected_service_id=SERVICE, body=body, now=1000)
    return verify_issuer_request(**{**defaults, **kwargs})


def test_service_proof_binds_full_request_recipient_operation_and_identity():
    body = signed()
    assert verify(body).allowed
    for key, value in [("bundle_id", "other"), ("operation", "other"),
                       ("expected_service_id", "other"), ("secret", "y" * 32)]:
        assert not verify(body, **{key: value}).allowed
    for key, value in [("actor_subject", "other"), ("context_ref", "forged"),
                       ("change_digest", "0" * 64), ("card_revision", 4),
                       ("request_id", "other"), ("action", "revoke"),
                       ("access_id", "other"), ("issuer_kind", "other"),
                       ("issuer_ref", "other")]:
        forged = {**body, "request": {**body["request"], key: value}}
        assert not verify(forged).allowed


def test_each_check_gets_a_fresh_nonce_and_short_secret_cannot_verify():
    assert signed()["service_proof"]["nonce"] != signed()["service_proof"]["nonce"]
    assert not verify(signed(), secret="short").allowed
    assert not verify({"request": asdict(REQUEST)}).allowed
    assert not verify(signed(), now=100000).allowed


def test_parser_refuses_unknown_or_untyped_fields():
    assert issuer_request_from_mapping(asdict(REQUEST)) == REQUEST
    for raw in ({**asdict(REQUEST), "_seal": "forged"},
                {**asdict(REQUEST), "card_revision": True},
                {**asdict(REQUEST), "context_ref": ""}):
        with pytest.raises(ValueError):
            issuer_request_from_mapping(raw)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["echo", "unsealed", "transport", "expiry", "timeout", "refusal"])
async def test_invalid_remote_responses_fail_closed(failure):
    now = datetime.now(timezone.utc)

    async def transport(request):
        if failure == "transport":
            raise RuntimeError("sensitive credentials")
        if failure == "timeout":
            import asyncio
            await asyncio.sleep(0.1)
        return {
            "ok": failure != "refusal",
            "request": asdict(replace(REQUEST, actor_subject="other")) if failure == "echo" else request,
            "decision": {"allowed": True, "reason": "", "policy_version": "policy",
                         "valid_until": (now + timedelta(seconds=61 if failure == "expiry" else 30)).isoformat(),
                         **({"_seal": "forged"} if failure == "unsealed" else {})},
        }

    registry = IssuerRegistry(now=lambda: now)
    registry.register(RemoteIssuerAdapter(issuer_kind=REQUEST.issuer_kind, adapter_id="peer",
                                         transport=transport, timeout_seconds=0.01))
    result = await registry.decide(REQUEST)
    assert not result.allowed
    assert "sensitive" not in result.reason


@pytest.mark.asyncio
async def test_valid_remote_answer_is_locally_sealed_and_freshly_requested_twice():
    now = datetime.now(timezone.utc)
    calls = []

    async def transport(request):
        calls.append(request)
        return {"ok": True, "request": request, "decision": {
            "allowed": True, "reason": "", "policy_version": "policy",
            "valid_until": (now + timedelta(seconds=30)).isoformat(),
        }}

    registry = IssuerRegistry(now=lambda: now)
    registry.register(RemoteIssuerAdapter(issuer_kind=REQUEST.issuer_kind, adapter_id="peer", transport=transport))
    issued = await registry.decide(REQUEST)
    assert issued.allowed
    assert (await registry.revalidate(REQUEST, issued)).allowed
    assert calls == [asdict(REQUEST), asdict(REQUEST)]


@pytest.mark.parametrize("protocol", ["issuer-context.v1", "issuer-outcome.v1"])
def test_context_and_outcome_envelopes_bind_all_evidence_and_domain(protocol):
    payload = {"request": asdict(replace(REQUEST, context_ref="") if protocol == "issuer-context.v1" else REQUEST),
               **({"current": {"access_id": "access"}, "candidate": {"label": "new"}}
                  if protocol == "issuer-context.v1" else {"outcome": {"state": "committed", "card_revision": 4}})}
    body = sign_issuer_envelope(secret=SECRET, bundle_id=BUNDLE, operation=OPERATION,
                                service_id=SERVICE, protocol=protocol, payload=payload, now=1000)
    defaults = dict(secret=SECRET, bundle_id=BUNDLE, operation=OPERATION,
                    expected_service_id=SERVICE, protocol=protocol, now=1000)
    assert verify_issuer_envelope(**defaults, body=body).allowed
    forged = {**body, "candidate": {"label": "wider"}}
    assert not verify_issuer_envelope(**defaults, body=forged).allowed
    assert not verify_issuer_envelope(**{**defaults, "operation": "other"}, body=body).allowed
    assert not verify_issuer_envelope(**{**defaults, "expected_service_id": "other"}, body=body).allowed
    assert not verify_issuer_envelope(**{**defaults, "protocol": "issuer-outcome.v1" if protocol == "issuer-context.v1" else "issuer-context.v1"}, body=body).allowed


@pytest.mark.asyncio
async def test_remote_prepare_then_two_reads_then_exact_terminal_outcome():
    now = datetime.now(timezone.utc)
    calls = []

    async def prepare(payload):
        calls.append(("prepare", payload))
        return {"ok": True, "request": payload["request"],
                "change_digest": payload["request"]["change_digest"], "context_ref": "server-reservation"}

    async def decide(payload):
        calls.append(("decide", payload))
        return {"ok": True, "request": payload, "decision": {
            "allowed": True, "reason": "", "policy_version": "policy",
            "valid_until": (now + timedelta(seconds=30)).isoformat(),
        }}

    async def finalize(payload):
        calls.append(("finalize", payload))
        return {"ok": True, **payload, "finalized": True}

    registry = IssuerRegistry(now=lambda: now)
    registry.register(RemoteIssuerAdapter(issuer_kind=REQUEST.issuer_kind, adapter_id="peer",
                                         transport=decide, prepare_transport=prepare, finalize_transport=finalize))
    prepared = await registry.prepare(replace(REQUEST, context_ref=""), current={}, candidate={"label": "new"})
    issued = await registry.decide(prepared)
    assert (await registry.revalidate(prepared, issued)).allowed
    assert await registry.finalize(prepared, state="committed", card_revision=4)
    assert [kind for kind, _ in calls] == ["prepare", "decide", "decide", "finalize"]


@pytest.mark.parametrize("outcome", [
    {"state": "committed", "card_revision": True},
    {"state": "committed", "card_revision": "1"},
    {"state": "committed", "card_revision": 0},
    {"state": "granted", "card_revision": 1},
    {"state": "committed", "card_revision": 1, "allowed": True},
])
def test_outcome_envelope_refuses_untyped_or_granting_outcome(outcome):
    with pytest.raises(ValueError, match="issuer_envelope_invalid"):
        sign_issuer_envelope(secret=SECRET, bundle_id=BUNDLE, operation=OPERATION,
                             service_id=SERVICE, protocol="issuer-outcome.v1",
                             payload={"request": asdict(REQUEST), "outcome": outcome})


@pytest.mark.asyncio
async def test_finalize_bool_revision_cannot_masquerade_as_exact_integer_echo():
    async def finalize(payload):
        return {"ok": True, "request": payload["request"], "finalized": True,
                "outcome": {"state": "committed", "card_revision": True}}

    registry = IssuerRegistry()
    registry.register(RemoteIssuerAdapter(issuer_kind=REQUEST.issuer_kind, adapter_id="peer",
                                         transport=finalize, finalize_transport=finalize))
    assert not await registry.finalize(REQUEST, state="committed", card_revision=1)


@pytest.mark.parametrize("protocol", ["issuer-context.v1", "issuer-decision.v1", "issuer-outcome.v1"])
def test_real_full_card_non_ascii_evidence_uses_bounded_admission_digest(protocol):
    from connection_hub.delegated_credentials.issuer_gate import change_digest

    current = replace(_regular_control(), issuer_kind="opaque", issuer_ref="opaque/" + "ü" * 256)
    candidate = replace(current, card_revision=current.card_revision + 1, label="编辑 — ü",
                        properties={**current.properties, "non_ascii_evidence": "权限" * 300}).to_dict()
    request = replace(REQUEST, access_id=current.access_id, card_revision=current.card_revision,
                      issuer_ref=current.issuer_ref, request_id="wire-real-card",
                      change_digest=change_digest(candidate),
                      context_ref="" if protocol == "issuer-context.v1" else "server-reservation")
    payload = asdict(request) if protocol == "issuer-decision.v1" else {
        "request": asdict(request),
        **({"current": current.to_dict(), "candidate": candidate} if protocol == "issuer-context.v1"
           else {"outcome": {"state": "committed", "card_revision": candidate["card_revision"]}}),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True, allow_nan=False).encode("utf-8")
    assert len(canonical) > 512  # reproduces the earlier admission-context defect
    expected_digest = hashlib.sha256(canonical).hexdigest()
    assert issuer_payload_digest(payload) == expected_digest
    signing = dict(secret=SECRET, bundle_id=BUNDLE, operation=OPERATION, service_id=SERVICE, now=1000)
    verification = dict(secret=SECRET, bundle_id=BUNDLE, operation=OPERATION,
                        expected_service_id=SERVICE, now=1000)
    if protocol == "issuer-decision.v1":
        body = sign_issuer_request(**signing, request=request)
        verification_fn = verify_issuer_request
    else:
        body = sign_issuer_envelope(**signing, protocol=protocol, payload=payload)
        verification["protocol"] = protocol
        verification_fn = verify_issuer_envelope
    assert verification_fn(**verification, body=body).allowed
    # Independent construction exercises real admission validation and pins
    # the wire contract rather than sign/verify sharing the same mistake.
    admission = AdmissionRequest(resource=BUNDLE, operation=OPERATION,
                                 invocation_id=request.request_id, request_digest=expected_digest,
                                 approval_context={"protocol": protocol})
    assert admission.validation_error() == ""
    assert verify_admission_request(secret=SECRET, proof=ServiceProof(**body["service_proof"]),
        delegated_token=f"{protocol}:{request.request_id}", request=admission, now=1000).allowed
    assert not verification_fn(**{**verification, "bundle_id": "other-authority"}, body=body).allowed
    assert not verification_fn(**{**verification, "operation": "other-target"}, body=body).allowed
    assert not verification_fn(**{**verification, "expected_service_id": "other-service"}, body=body).allowed
    # Replay to a different recipient/domain cannot reuse the captured proof.
    assert not verification_fn(**{**verification, "now": 100000}, body=body).allowed
    fields = {"actor_subject": "other", "request_id": "other-request", "action": "revoke",
              "access_id": "other-card", "card_revision": current.card_revision + 1,
              "issuer_kind": "other", "issuer_ref": "other", "change_digest": "0" * 64,
              "context_ref": "forged"}
    for name, value in fields.items():
        forged = copy.deepcopy(body)
        forged["request"][name] = value
        assert not verification_fn(**verification, body=forged).allowed, name
    if protocol == "issuer-context.v1":
        for key in ("current", "candidate"):
            forged = copy.deepcopy(body)
            forged[key]["label"] = "wider"
            assert not verification_fn(**verification, body=forged).allowed
        assert not verification_fn(**verification, body={
            "request": body["request"], "change_digest": request.change_digest,
            "service_proof": body["service_proof"],
        }).allowed  # digest alone is not actual Card evidence
    if protocol == "issuer-outcome.v1":
        forged = copy.deepcopy(body)
        forged["outcome"]["state"] = "refused"
        assert not verification_fn(**verification, body=forged).allowed


@pytest.mark.parametrize("request_id", ["x" * 257, "non ascii ü", "line\nbreak"])
def test_signer_preserves_existing_bounded_invocation_validation(request_id):
    request = replace(REQUEST, request_id=request_id)
    with pytest.raises(ValueError, match="invocation_id_invalid"):
        sign_issuer_request(secret=SECRET, bundle_id=BUNDLE, operation=OPERATION,
                            service_id=SERVICE, request=request)
    with pytest.raises(ValueError, match="invocation_id_invalid"):
        sign_issuer_envelope(secret=SECRET, bundle_id=BUNDLE, operation=OPERATION,
            service_id=SERVICE, protocol="issuer-outcome.v1",
            payload={"request": asdict(request), "outcome": {"state": "committed", "card_revision": 4}})


def test_maximum_bounded_invocation_id_and_large_context_reference_verify():
    request = replace(REQUEST, request_id="x" * 256, context_ref="server/" + "c" * 4096)
    body = sign_issuer_request(secret=SECRET, bundle_id=BUNDLE, operation=OPERATION,
                               service_id=SERVICE, request=request, now=1000)
    assert verify(body).allowed


@pytest.mark.parametrize("field", ["invocation_id", "request_digest"])
@pytest.mark.parametrize("protocol", ["issuer-decision.v1", "issuer-context.v1", "issuer-outcome.v1"])
def test_independently_signed_wrong_invocation_or_payload_digest_is_refused(field, protocol):
    request = replace(REQUEST, context_ref="" if protocol == "issuer-context.v1" else REQUEST.context_ref)
    payload = asdict(request) if protocol == "issuer-decision.v1" else {
        "request": asdict(request),
        **({"current": {}, "candidate": {"label": "ü"}} if protocol == "issuer-context.v1"
           else {"outcome": {"state": "committed", "card_revision": 4}}),
    }
    signing = dict(secret=SECRET, bundle_id=BUNDLE, operation=OPERATION,
                   service_id=SERVICE, now=1000, nonce="nonce-1234567890abcd")
    if protocol == "issuer-decision.v1":
        body = sign_issuer_request(**signing, request=request)
    else:
        body = sign_issuer_envelope(**signing, protocol=protocol, payload=payload)
    wrong = AdmissionRequest(resource=BUNDLE, operation=OPERATION,
        invocation_id="other-invocation" if field == "invocation_id" else request.request_id,
        request_digest="0" * 64 if field == "request_digest" else issuer_payload_digest(payload),
        approval_context={"protocol": protocol})
    assert not wrong.validation_error()  # shape-valid, cryptographically mismatched
    body["service_proof"]["signature"] = sign_admission_request(
        secret=SECRET, service_id=SERVICE, timestamp="1000", nonce=signing["nonce"],
        delegated_token=f"{protocol}:{request.request_id}", request=wrong)
    if protocol == "issuer-decision.v1":
        assert not verify(body).allowed
    else:
        assert not verify_issuer_envelope(secret=SECRET, bundle_id=BUNDLE, operation=OPERATION,
            expected_service_id=SERVICE, protocol=protocol, body=body, now=1000).allowed
