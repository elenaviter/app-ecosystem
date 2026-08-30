from __future__ import annotations

from connection_hub.authority_registry import CredentialEnvelope
from connection_hub.delegated_credentials.admission import (
    AdmissionAccount,
    AdmissionConfig,
    AdmissionRequest,
    ServiceProof,
    admission_allow,
    authorize_account_scope,
    pairwise_service_subject,
    sign_admission_request,
    verify_admission_request,
)
from connection_hub.delegated_credentials.credential_view import DelegatedCredentialView


SECRET = "service-secret-with-at-least-thirty-two-bytes"
SUBJECT_SECRET = "projection-secret-with-at-least-thirty-two-bytes"
RESOURCE = "https://service.example/customers"


def _request(*, claims=()) -> AdmissionRequest:
    return AdmissionRequest(
        resource=RESOURCE,
        operation="customers.search",
        account=AdmissionAccount(
            provider_id="salesforce",
            account_id="account-17",
            claims=tuple(claims),
        ),
    )


def _view() -> DelegatedCredentialView:
    return DelegatedCredentialView.from_envelope(
        CredentialEnvelope(
            credential_kind="delegated_client_access",
            issuer_authority_id="delegated_client",
            subject="integration:client:user-1",
            attrs={
                "client_id": "external-client",
                "grantor_subject": "user-1",
                "resource_grants": {RESOURCE: ["crm:read"]},
                "account_scope": {
                    "salesforce": {"account-17": ["contacts:read"]}
                },
            },
        ),
        {
            "registry_access_id": "access-1",
            "card_revision": 7,
            "catalog_version": "catalog-1",
            "account_scope": {
                "salesforce": {"account-17": ["contacts:read"]}
            },
        },
    )


def test_admission_config_binds_services_to_catalog_resources_only() -> None:
    config = AdmissionConfig.from_connections(
        {
            "delegated_credentials": {
                "admission": {
                    "enabled": True,
                    "identity_projection_secret_ref": "admission.subject_secret",
                    "max_clock_skew_seconds": 120,
                    "nonce_ttl_seconds": 100,
                    "services": {
                        "crm-api": {
                            "secret_ref": "admission.services.crm-api.secret",
                            "resources": ["https://service.example/*"],
                        }
                    },
                }
            }
        }
    )

    assert config.enabled
    assert config.nonce_ttl_seconds == 240
    service = config.service("crm-api")
    assert service is not None
    assert service.allows_resource(RESOURCE)
    assert not service.allows_resource("https://other.example/customers")


def test_signed_admission_binds_service_token_and_semantic_request() -> None:
    request = _request(claims=("contacts:read",))
    signature = sign_admission_request(
        secret=SECRET,
        service_id="crm-api",
        timestamp="1000",
        nonce="nonce-1234567890abcd",
        delegated_token="kst1.token",
        request=request,
    )
    proof = ServiceProof(
        service_id="crm-api",
        timestamp="1000",
        nonce="nonce-1234567890abcd",
        signature=signature,
    )

    assert verify_admission_request(
        secret=SECRET,
        proof=proof,
        delegated_token="kst1.token",
        request=request,
        now=1000,
    ).allowed
    assert verify_admission_request(
        secret=SECRET,
        proof=proof,
        delegated_token="kst1.other",
        request=request,
        now=1000,
    ).reason == "signature_invalid"
    assert verify_admission_request(
        secret=SECRET,
        proof=proof,
        delegated_token="kst1.token",
        request=request,
        now=1400,
    ).reason == "timestamp_outside_window"


def test_service_proof_reads_case_insensitive_wire_headers() -> None:
    proof = ServiceProof.from_headers(
        {
            "X-Connection-Hub-Service-Id": "crm-api",
            "X-Connection-Hub-Timestamp": "1000",
            "X-Connection-Hub-Nonce": "nonce-1234567890abcd",
            "X-Connection-Hub-Signature": "signature",
        }
    )

    assert proof == ServiceProof(
        service_id="crm-api",
        timestamp="1000",
        nonce="nonce-1234567890abcd",
        signature="signature",
    )


def test_service_proof_accepts_pre_rename_header_aliases() -> None:
    proof = ServiceProof.from_headers(
        {
            "X-Prokura-Service-Id": "crm-api",
            "X-Prokura-Timestamp": "1000",
            "X-Prokura-Nonce": "nonce-1234567890abcd",
            "X-Prokura-Signature": "signature",
        }
    )

    assert proof == ServiceProof(
        service_id="crm-api",
        timestamp="1000",
        nonce="nonce-1234567890abcd",
        signature="signature",
    )


def test_short_service_secret_is_an_invalid_service_configuration() -> None:
    request = _request()
    decision = verify_admission_request(
        secret="too-short",
        proof=ServiceProof(
            service_id="crm-api",
            timestamp="1000",
            nonce="nonce-1234567890abcd",
            signature="irrelevant",
        ),
        delegated_token="kst1.token",
        request=request,
        now=1000,
    )

    assert decision.reason == "service_secret_too_short"


def test_requested_account_is_narrowed_by_the_live_card() -> None:
    allowed = authorize_account_scope(_view(), _request(claims=("contacts:read",)).account)
    denied_claim = authorize_account_scope(
        _view(), _request(claims=("contacts:write",)).account
    )
    denied_account = authorize_account_scope(
        _view(),
        AdmissionAccount(
            provider_id="salesforce",
            account_id="account-99",
            claims=("contacts:read",),
        ),
    )

    assert allowed.allowed
    assert allowed.claims == ("contacts:read",)
    assert denied_claim.reason == "account_claim_not_granted"
    assert denied_account.reason == "account_not_granted"


def test_allow_projection_is_pairwise_and_contains_no_internal_user_id() -> None:
    view = _view()
    subject = pairwise_service_subject(
        secret=SUBJECT_SECRET,
        service_id="crm-api",
        grantor_user_id="user-1",
    )
    response = admission_allow(
        decision_id="decision-1",
        service_id="crm-api",
        subject=subject,
        view=view,
        request=_request(claims=("contacts:read",)),
        available_grants=("crm:read",),
        account_claims=("contacts:read",),
        expires_at=2000,
        active_catalog_version="catalog-2",
    )

    assert response["allowed"] is True
    assert response["principal"]["sub"].startswith("prk_sub_")
    assert "user-1" not in str(response)
    assert response["provenance"]["card_revision"] == 7
