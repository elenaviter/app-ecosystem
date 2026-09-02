from __future__ import annotations

from fastapi.testclient import TestClient

from reference_service.admission_client import AdmissionResult
from reference_service.app import create_app
from reference_service.settings import Settings
from connection_hub.invocation_policy import canonical_request_digest


SETTINGS = Settings(
    admission_url="https://hub.example/admission",
    service_id="reference-customers-api",
    service_secret="reference-service-secret-at-least-thirty-two-bytes",
    resource="https://reference.example.test/customers",
)


class StubAdmission:
    def __init__(self, result: AdmissionResult) -> None:
        self.result = result
        self.calls: list[dict[str, str]] = []

    async def evaluate(
        self,
        *,
        delegated_bearer: str,
        operation: str,
        invocation_id: str,
        request_digest: str,
    ) -> AdmissionResult:
        self.calls.append(
            {
                "delegated_bearer": delegated_bearer,
                "operation": operation,
                "invocation_id": invocation_id,
                "request_digest": request_digest,
            }
        )
        return self.result


def test_endpoint_requires_delegated_bearer() -> None:
    admission = StubAdmission(AdmissionResult(200, {"allowed": True}))
    client = TestClient(create_app(settings=SETTINGS, admission=admission))

    response = client.post("/customers/search", json={"query": "north"})

    assert response.status_code == 401
    assert admission.calls == []


def test_endpoint_releases_domain_data_only_after_allow() -> None:
    admission = StubAdmission(
        AdmissionResult(
            200,
            {
                "ok": True,
                "allowed": True,
                "principal": {"sub": "prk_sub_example"},
            },
        )
    )
    client = TestClient(create_app(settings=SETTINGS, admission=admission))

    response = client.post(
        "/customers/search",
        headers={
            "Authorization": "Bearer kst1.example",
            "Idempotency-Key": "customer-search-1",
        },
        json={"query": "north"},
    )

    assert response.status_code == 200
    assert response.json()["customers"] == [
        {"id": "customer-101", "name": "Northwind Labs", "status": "active"}
    ]
    assert response.json()["invocation_id"] == "customer-search-1"
    assert admission.calls == [
        {
            "delegated_bearer": "kst1.example",
            "operation": "customers.search",
            "invocation_id": "customer-search-1",
            "request_digest": canonical_request_digest({"query": "north"}),
        }
    ]


def test_endpoint_passes_current_authority_denial_through() -> None:
    admission = StubAdmission(
        AdmissionResult(
            403,
            {
                "ok": False,
                "allowed": False,
                "error": {"code": "delegated_operation_not_granted"},
            },
        )
    )
    client = TestClient(create_app(settings=SETTINGS, admission=admission))

    response = client.post(
        "/customers/search",
        headers={
            "Authorization": "Bearer kst1.example",
            "Idempotency-Key": "customer-search-denied",
        },
        json={"query": "north"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "delegated_operation_not_granted"
