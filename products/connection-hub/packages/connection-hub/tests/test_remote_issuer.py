from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone

import pytest

from connection_hub.delegated_credentials.issuer_gate import IssuerRegistry
from connection_hub.delegated_credentials.remote_issuer import (
    RemoteIssuerAdapter, issuer_request_from_mapping, sign_issuer_request,
    verify_issuer_request,
    sign_issuer_envelope, verify_issuer_envelope,
)
from test_issuer_gate import REQUEST

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
                       ("change_digest", "0" * 64), ("card_revision", 4)]:
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
