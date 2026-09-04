from __future__ import annotations

import pytest
from connection_hub_cli.authorization.models import (
    AuthorizationServerMetadata,
    OAuthClientRegistration,
    OAuthTokenSet,
    ProtectedResourceMetadata,
    authorization_server_metadata_url,
)
from connection_hub_cli.authorization.pkce import code_challenge, generate_pkce
from connection_hub_cli.errors import AuthorizationError


def _server_metadata(**updates):
    value = {
        "issuer": "https://auth.example.test",
        "authorization_endpoint": "https://auth.example.test/oauth/authorize",
        "token_endpoint": "https://auth.example.test/oauth/token",
        "registration_endpoint": "https://auth.example.test/oauth/register",
        "revocation_endpoint": "https://auth.example.test/oauth/revoke",
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "authorization_response_iss_parameter_supported": True,
        "scopes_supported": ["management.read", "management.reload"],
    }
    value.update(updates)
    return value


def test_protected_resource_requires_exact_resource_and_one_server() -> None:
    result = ProtectedResourceMetadata.from_mapping(
        {
            "resource": "https://runtime.example.test/management",
            "authorization_servers": ["https://auth.example.test"],
            "scopes_supported": ["management.read"],
        },
        expected_resource="https://runtime.example.test/management",
    )

    assert result.authorization_server == "https://auth.example.test"
    assert result.scopes_supported == ("management.read",)

    with pytest.raises(AuthorizationError) as mismatch:
        ProtectedResourceMetadata.from_mapping(
            {
                "resource": "https://other.example.test/management",
                "authorization_servers": ["https://auth.example.test"],
            },
            expected_resource="https://runtime.example.test/management",
        )
    assert mismatch.value.code == "oauth_resource_mismatch"

    with pytest.raises(AuthorizationError) as several:
        ProtectedResourceMetadata.from_mapping(
            {
                "resource": "https://runtime.example.test/management",
                "authorization_servers": [
                    "https://auth-a.example.test",
                    "https://auth-b.example.test",
                ],
            },
            expected_resource="https://runtime.example.test/management",
        )
    assert several.value.code == "oauth_authorization_server_selection_required"


def test_authorization_server_requires_exact_issuer_pkce_and_public_client() -> None:
    result = AuthorizationServerMetadata.from_mapping(
        _server_metadata(),
        expected_issuer="https://auth.example.test",
    )
    assert result.supports_refresh is True
    assert result.authorization_response_issuer_required is True

    with pytest.raises(AuthorizationError) as issuer:
        AuthorizationServerMetadata.from_mapping(
            _server_metadata(issuer="https://wrong.example.test"),
            expected_issuer="https://auth.example.test",
        )
    assert issuer.value.code == "oauth_issuer_mismatch"

    with pytest.raises(AuthorizationError) as pkce:
        AuthorizationServerMetadata.from_mapping(
            _server_metadata(code_challenge_methods_supported=["plain"]),
            expected_issuer="https://auth.example.test",
        )
    assert pkce.value.code == "oauth_pkce_unsupported"

    with pytest.raises(AuthorizationError) as confidential:
        AuthorizationServerMetadata.from_mapping(
            _server_metadata(
                token_endpoint_auth_methods_supported=["client_secret_basic"]
            ),
            expected_issuer="https://auth.example.test",
        )
    assert confidential.value.code == "oauth_public_client_unsupported"


def test_oauth_urls_allow_https_and_loopback_http_only() -> None:
    assert authorization_server_metadata_url("https://auth.example/a") == (
        "https://auth.example/a/.well-known/oauth-authorization-server"
    )
    assert authorization_server_metadata_url("http://127.0.0.1:8020") == (
        "http://127.0.0.1:8020/.well-known/oauth-authorization-server"
    )

    with pytest.raises(AuthorizationError):
        authorization_server_metadata_url("http://auth.example.test")


def test_management_resource_accepts_an_absolute_urn() -> None:
    resource = ProtectedResourceMetadata.from_mapping(
        {
            "resource": "urn:kdcube:management:deployment:tenant-a:project-a",
            "authorization_servers": ["https://auth.example.test"],
        },
        expected_resource="urn:kdcube:management:deployment:tenant-a:project-a",
    )
    assert resource.resource == ("urn:kdcube:management:deployment:tenant-a:project-a")


def test_client_registration_rejects_callback_substitution() -> None:
    with pytest.raises(AuthorizationError) as raised:
        OAuthClientRegistration.from_mapping(
            {
                "client_id": "registered-client",
                "redirect_uris": ["http://127.0.0.1:9000/other"],
                "token_endpoint_auth_method": "none",
            },
            expected_redirect_uri="http://127.0.0.1:9000/callback",
        )
    assert raised.value.code == "oauth_registered_callback_mismatch"


def test_token_repr_is_redacted_and_refresh_rotation_is_preserved() -> None:
    token = OAuthTokenSet.from_mapping(
        {
            "access_token": "access-secret-marker",
            "refresh_token": "refresh-secret-marker",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "management.read",
            "access_id": "access_123",
        },
        now=1000,
    )
    rendered = repr(token)

    assert "access-secret-marker" not in rendered
    assert "refresh-secret-marker" not in rendered
    assert token.expires_at == 4600
    assert token.is_expiring(now=4500, leeway_seconds=100) is True

    refreshed = OAuthTokenSet.from_mapping(
        {"access_token": "new-access", "expires_in": 60},
        previous_refresh_token=token.refresh_token,
        now=2000,
    )
    assert refreshed.refresh_token == "refresh-secret-marker"


@pytest.mark.parametrize(
    "payload",
    [
        {"access_token": "token-\u2603", "expires_in": 60},
        {"access_token": "access", "refresh_token": "refresh-\u2603"},
        {"access_token": "access", "scope": "scope-\u2603"},
        {"access_token": 123, "expires_in": 60},
        {"access_token": "access", "refresh_token": 123},
        {"access_token": "access", "scope": ["mcp"]},
    ],
)
def test_token_record_rejects_values_outside_the_oauth_ascii_domain(payload) -> None:
    with pytest.raises(AuthorizationError) as raised:
        OAuthTokenSet.from_mapping(payload)

    assert raised.value.code == "oauth_token_response_invalid"


def test_pkce_uses_the_standard_s256_transformation() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert code_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"

    generated = generate_pkce()
    assert 43 <= len(generated.code_verifier) <= 128
    assert generated.code_challenge == code_challenge(generated.code_verifier)
    assert len(generated.state) >= 32
