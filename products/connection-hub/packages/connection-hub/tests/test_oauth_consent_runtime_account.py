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

    html = render_consent_html(_request(client), "https://hub.example.test")

    assert "Provider account" in html
    assert "Codex · worker@example.test" in html
    assert "acct-runtime-42" in html
    assert "org-runtime" in html
    assert "Reported by the host" in html
    assert "Access comes from the Card choices approved below" in html


def test_consent_omits_runtime_account_block_without_an_account_id() -> None:
    client = PublicClient(
        client_id="dcr-worker",
        redirect_uris=("http://127.0.0.1/callback",),
        client_metadata={"kdcube_agent_provider": "codex"},
    )

    html = render_consent_html(_request(client), "https://hub.example.test")

    assert "Provider account" not in html
    assert "Reported by the host" not in html
