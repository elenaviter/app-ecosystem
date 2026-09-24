from __future__ import annotations

from connection_hub.delegated_credentials.oauth.clients import PublicClient
from connection_hub.delegated_credentials.oauth.consent import render_consent_html
from connection_hub.delegated_credentials.oauth.flow import AuthorizeRequest


def _request(client: PublicClient) -> AuthorizeRequest:
    return AuthorizeRequest(
        client_id=client.client_id,
        redirect_uri=client.redirect_uris[0],
        response_type="code",
        scopes=[],
        state="state",
        code_challenge="challenge",
        code_challenge_method="S256",
        client=client,
    )


def test_consent_shows_host_reported_runtime_account_as_identification() -> None:
    client = PublicClient(
        client_id="dcr-worker",
        redirect_uris=("http://127.0.0.1/callback",),
        client_name="Problem Board worker",
        client_metadata={
            "kdcube_agent_provider": "codex",
            "kdcube_agent_account": {
                "account_id": "acct-runtime-42",
                "email": "worker@example.test",
                "organization": "org-runtime",
            },
        },
    )

    html = render_consent_html(
        _request(client),
        "https://hub.example.test",
        grantor_subject="user-42",
        grantor_label="Elena Example",
    )

    assert "Owner" in html
    assert "Elena Example" in html
    assert "Provider account" in html
    assert "Codex · worker@example.test" in html
    assert "Account ID" in html
    assert "acct-runtime-42" in html
    assert "Organization ID" in html
    assert "org-runtime" in html
    assert "Read from the Codex login on this host" in html
    assert "It identifies the agent's provider account" in html
    assert "Access comes from the owner's Card" in html
    assert ">user-42</" not in html


def test_consent_does_not_call_an_account_without_email_unreported() -> None:
    client = PublicClient(
        client_id="dcr-worker",
        redirect_uris=("http://127.0.0.1/callback",),
        client_metadata={
            "kdcube_agent_provider": "codex",
            "kdcube_agent_account": {"account_id": "acct-runtime-42"},
        },
    )

    html = render_consent_html(_request(client), "https://hub.example.test")

    assert "Provider account" in html
    assert "<strong>Codex</strong>" in html
    assert "Codex · Not reported" not in html


def test_consent_shows_not_reported_without_an_account_id() -> None:
    client = PublicClient(
        client_id="dcr-worker",
        redirect_uris=("http://127.0.0.1/callback",),
        client_metadata={"kdcube_agent_provider": "codex"},
    )

    html = render_consent_html(_request(client), "https://hub.example.test")

    assert "Provider account" in html
    assert "Codex · Not reported" in html
    assert "No provider account was reported by this host" in html


def test_consent_omits_provider_account_for_an_unrelated_client() -> None:
    client = PublicClient(
        client_id="dcr-application",
        redirect_uris=("http://127.0.0.1/callback",),
        client_name="Connected application",
        client_metadata={},
    )

    html = render_consent_html(_request(client), "https://hub.example.test")

    assert "Provider account" not in html
    assert "Not reported" not in html
