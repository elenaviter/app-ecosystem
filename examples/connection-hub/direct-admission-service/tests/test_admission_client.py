from __future__ import annotations

import json

import httpx
import pytest
from connection_hub.delegated_credentials.admission import (
    AdmissionRequest,
    ServiceProof,
    verify_admission_request,
)

from reference_service.admission_client import AdmissionClient
from reference_service.settings import Settings


SECRET = "reference-service-secret-at-least-thirty-two-bytes"


@pytest.mark.asyncio
async def test_client_sends_independent_valid_workload_proof() -> None:
    seen_nonces: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        delegated_bearer = request.headers["authorization"].removeprefix("Bearer ")
        admission_request = AdmissionRequest.from_mapping(json.loads(request.content))
        proof = ServiceProof.from_headers(request.headers)
        verified = verify_admission_request(
            secret=SECRET,
            proof=proof,
            delegated_token=delegated_bearer,
            request=admission_request,
        )
        assert verified.allowed
        assert admission_request.operation == "customers.search"
        seen_nonces.append(proof.nonce)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "allowed": True,
                "principal": {"sub": "prk_sub_example"},
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = AdmissionClient(
            Settings(
                admission_url="https://hub.example/admission",
                service_id="reference-customers-api",
                service_secret=SECRET,
                resource="https://reference.example.test/customers",
            ),
            client=http_client,
        )
        first = await client.evaluate(
            delegated_bearer="kst1.example",
            operation="customers.search",
        )
        second = await client.evaluate(
            delegated_bearer="kst1.example",
            operation="customers.search",
        )

    assert first.allowed and second.allowed
    assert len(set(seen_nonces)) == 2


@pytest.mark.asyncio
async def test_client_preserves_structured_denial() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            403,
            json={
                "ok": False,
                "allowed": False,
                "error": {"code": "delegated_operation_not_granted"},
            },
        )
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = AdmissionClient(
            Settings(
                admission_url="https://hub.example/admission",
                service_id="reference-customers-api",
                service_secret=SECRET,
                resource="https://reference.example.test/customers",
            ),
            client=http_client,
        )
        decision = await client.evaluate(
            delegated_bearer="kst1.example",
            operation="customers.search",
        )

    assert decision.status_code == 403
    assert decision.payload["error"]["code"] == "delegated_operation_not_granted"
