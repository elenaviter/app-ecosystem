from __future__ import annotations

import logging

import pytest

from connection_hub.delegated_credentials.consent_denial import (
    connection_hub_grant_url,
    connection_hub_invocation_policy_url,
)
from connection_hub.delegated_to_kdcube import public_base
from connection_hub.delegated_to_kdcube.public_base import (
    set_connection_hub_public_base_url,
)
from connection_hub.remote_mcp.proxy import RemoteMCPProxyError

_BASE = "https://hub.example.test"
_PATH_PREFIX = "/api/integrations/bundles/acme/main/connection-hub%401-0/widgets/"


@pytest.fixture(autouse=True)
def _reset_public_base():
    set_connection_hub_public_base_url("")
    public_base._warned_relative = False
    yield
    set_connection_hub_public_base_url("")
    public_base._warned_relative = False


def _grant_url() -> str:
    return connection_hub_grant_url(
        tenant="acme",
        project="main",
        client_id="dcr-client",
        resource="*/remote_mcp_proxy*",
        claims=["external_mcp:use"],
        outer_operation="search",
    )


def _policy_url() -> str:
    return connection_hub_invocation_policy_url(
        tenant="acme",
        project="main",
        access_id="oauth-0123456789abcdef",
        resource="*/remote_mcp_proxy*",
        operation="search",
    )


@pytest.mark.parametrize("builder", [_grant_url, _policy_url])
def test_recovery_url_is_absolute_when_the_public_base_is_known(builder):
    set_connection_hub_public_base_url(_BASE)
    assert builder().startswith(f"{_BASE}{_PATH_PREFIX}")


@pytest.mark.parametrize("builder", [_grant_url, _policy_url])
def test_recovery_url_stays_relative_instead_of_empty_without_a_base(builder):
    """A worker that never seeded the base still names the destination.

    An empty string is indistinguishable from "no recovery exists", and the
    payload carries available_choices beside it.
    """
    url = builder()
    assert url.startswith(_PATH_PREFIX)


def test_a_missing_base_warns_once_per_process(caplog):
    with caplog.at_level(logging.WARNING, logger=public_base.__name__):
        _grant_url()
        _policy_url()
    warnings = [r for r in caplog.records if "stays RELATIVE" in r.getMessage()]
    assert len(warnings) == 1
    assert public_base.PUBLIC_BASE_URL_CONFIG_KEY in warnings[0].getMessage()


def test_an_unaddressable_card_still_yields_no_url():
    set_connection_hub_public_base_url(_BASE)
    assert connection_hub_invocation_policy_url(
        tenant="acme",
        project="main",
        access_id="",
        resource="*/remote_mcp_proxy*",
        operation="search",
    ) == ""


def _denial(recovery_url: str) -> dict:
    return RemoteMCPProxyError(
        "delegated_invocation_limit_exhausted",
        resource="*/remote_mcp_proxy*",
        operation="search",
        connector_id="mcp_0123456789abcdef01234567",
        proxy_name="mcp_0123456789abcdef01234567__search",
        consent_required=True,
        access_id="oauth-0123456789abcdef",
        card_revision=4,
        client_id="dcr-client",
        recovery_url=recovery_url,
    ).to_dict()


def test_a_denial_omits_the_recovery_link_it_does_not_have():
    """Relayed verbatim to an agent, an empty field reads as a destination."""
    consent = _denial("")["consent"]
    assert "connection_hub_url" not in consent
    assert consent["available_choices"] == ["allow_once", "allow_always"]


def test_a_denial_withholds_a_link_the_relaying_client_cannot_open():
    """A relative path is useless to an agent relaying it outside the app origin."""
    consent = _denial(f"{_PATH_PREFIX}connections_settings?tab=x")["consent"]
    assert "connection_hub_url" not in consent


def test_a_denial_carries_the_recovery_link_it_has():
    consent = _denial(f"{_BASE}{_PATH_PREFIX}connections_settings?tab=x")["consent"]
    assert consent["connection_hub_url"].startswith(_BASE)
