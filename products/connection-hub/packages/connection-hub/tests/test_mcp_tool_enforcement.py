from __future__ import annotations

from types import SimpleNamespace

import pytest

from connection_hub.mcp_tool_enforcement import enforce_tool_requirements


class Credential:
    def __init__(self, *, ok: bool, claim: str, reason: str = "") -> None:
        self.ok = ok
        self.claim = claim
        self.reason = reason

    def error_envelope(self, *, where: str) -> dict:
        return {
            "ok": False,
            "where": where,
            "consent": {"reason": self.reason},
        }


def view(_request: object) -> object:
    return SimpleNamespace(
        grantor_user_id="user-1",
        agent_client_id="kdcube-agent:app:main",
        resource="*/app/public/mcp/tools*",
    )


@pytest.mark.asyncio
async def test_resolved_claim_allows_the_tool() -> None:
    seen: list[str] = []

    async def resolve(_source: object, *, claim: str, **_kwargs: object) -> Credential:
        seen.append(claim)
        return Credential(ok=True, claim=claim)

    result = await enforce_tool_requirements(
        object(),
        tool_name="mail_search",
        operation="search",
        requirements=[{"provider_id": "google", "claims": ["gmail:read"]}],
        identity={"user_id": "user-1"},
        resolution_source={"runtime": "test"},
        credential_resolver=resolve,
        credential_view_resolver=view,
    )

    assert result is None
    assert seen == ["gmail:read"]


@pytest.mark.asyncio
async def test_connect_first_denial_leads_when_no_account_exists() -> None:
    async def resolve(_source: object, *, claim: str, **_kwargs: object) -> Credential:
        return Credential(ok=False, claim=claim, reason="connect_required")

    seen: dict = {}

    async def connect_first(**kwargs: object) -> dict:
        seen.update(kwargs)
        return {"ok": False, "reason": "connect_required"}

    requirement = {"provider_id": "google", "claims": ["gmail:read"]}
    result = await enforce_tool_requirements(
        object(),
        tool_name="mail_search",
        operation="search",
        requirements=[requirement],
        identity={"user_id": "user-1", "tenant_id": "t", "project_id": "p"},
        resolution_source={},
        credential_resolver=resolve,
        credential_view_resolver=view,
        connect_first_denial_builder=connect_first,
    )

    assert result == {"ok": False, "reason": "connect_required"}
    assert seen["requirements"] == [requirement]
    assert seen["agent_client_id"] == "kdcube-agent:app:main"


@pytest.mark.asyncio
async def test_account_level_denial_is_preserved_when_accounts_exist() -> None:
    async def resolve(_source: object, *, claim: str, **_kwargs: object) -> Credential:
        return Credential(ok=False, claim=claim, reason="claim_upgrade_required")

    async def accounts_exist(**_kwargs: object) -> None:
        return None

    result = await enforce_tool_requirements(
        object(),
        tool_name="mail_search",
        operation="search",
        requirements=[{"provider_id": "google", "claims": ["gmail:read"]}],
        identity={"user_id": "user-1"},
        resolution_source={},
        credential_resolver=resolve,
        credential_view_resolver=view,
        connect_first_denial_builder=accounts_exist,
    )

    assert result["consent"]["reason"] == "claim_upgrade_required"


# ── any_of groups: a realm declared truthfully ──────────────────────────────

from connection_hub.mcp_tool_enforcement import resolve_tool_requirements
from connection_hub.delegated_to_kdcube.models import ToolClaimPolicy

MAIL_ANY_OF = {
    "any_of": [
        {"provider_id": "google", "claims": ["gmail:read"]},
        {"provider_id": "icloud_mail", "claims": ["email:read"]},
    ]
}


class AccountCredential(Credential):
    def __init__(self, *, ok: bool, claim: str, provider_id: str, account_id: str = "", reason: str = "") -> None:
        super().__init__(ok=ok, claim=claim, reason=reason)
        self.provider_id = provider_id
        self.account_id = account_id
        self.error_payload = {"reason": reason} if reason else {}


def _resolver(connected: dict[str, str]):
    """connected maps provider_id -> account_id that resolves; others fail with connect_required."""

    async def resolve(_source: object, *, provider_id: str, claim: str, account_id: str = "", **_kwargs: object):
        owned = connected.get(provider_id, "")
        if not owned:
            return AccountCredential(ok=False, claim=claim, provider_id=provider_id, reason="connect_required")
        if account_id and account_id != owned:
            return AccountCredential(ok=False, claim=claim, provider_id=provider_id, reason="account_not_found")
        return AccountCredential(ok=True, claim=claim, provider_id=provider_id, account_id=owned)

    return resolve


def test_any_of_group_parses_and_round_trips():
    policy = ToolClaimPolicy.from_tool_config(
        "mail_search", {"connections": {"delegated_to_kdcube": {"connected_accounts": [MAIL_ANY_OF]}}}
    )
    (requirement,) = policy.connected_accounts
    assert requirement.is_group and requirement.provider_id == ""
    assert [item.provider_id for item in requirement.alternatives()] == ["google", "icloud_mail"]
    assert requirement.to_dict() == MAIL_ANY_OF


@pytest.mark.asyncio
async def test_any_of_with_one_connected_provider_proceeds_and_names_it() -> None:
    resolution = await resolve_tool_requirements(
        object(),
        tool_name="mail_search",
        operation="search",
        requirements=[MAIL_ANY_OF],
        identity={"user_id": "user-1"},
        resolution_source={},
        credential_resolver=_resolver({"icloud_mail": "icloud_1"}),
        credential_view_resolver=view,
    )
    assert resolution.allowed
    chosen = resolution.account_for(0)
    assert chosen is not None
    assert (chosen.provider_id, chosen.account_id, chosen.any_of) == ("icloud_mail", "icloud_1", True)


@pytest.mark.asyncio
async def test_any_of_with_two_connected_providers_asks_with_labeled_candidates() -> None:
    async def accounts(provider_id: str):
        return [
            SimpleNamespace(account_id="google_1", email="lena@nestlogic.com", display_name=""),
            SimpleNamespace(account_id="icloud_1", email="elena.viter@icloud.com", display_name=""),
        ]

    resolution = await resolve_tool_requirements(
        object(),
        tool_name="mail_search",
        operation="search",
        requirements=[MAIL_ANY_OF],
        identity={"user_id": "user-1"},
        resolution_source={},
        credential_resolver=_resolver({"google": "google_1", "icloud_mail": "icloud_1"}),
        credential_view_resolver=view,
        accounts_lister=accounts,
    )
    assert not resolution.allowed
    assert resolution.denial["error"]["code"] == "account_required"
    labels = [row["label"] for row in resolution.denial["ret"]["candidates"]]
    assert labels == ["lena@nestlogic.com (google)", "elena.viter@icloud.com (icloud_mail)"]


@pytest.mark.asyncio
async def test_any_of_explicit_account_id_pins_its_provider() -> None:
    resolution = await resolve_tool_requirements(
        object(),
        tool_name="mail_get",
        operation="get",
        requirements=[MAIL_ANY_OF],
        account_id="icloud_1",
        identity={"user_id": "user-1"},
        resolution_source={},
        credential_resolver=_resolver({"google": "google_1", "icloud_mail": "icloud_1"}),
        credential_view_resolver=view,
    )
    assert resolution.allowed
    assert resolution.account_for(0).provider_id == "icloud_mail"


@pytest.mark.asyncio
async def test_any_of_with_nothing_connected_offers_every_alternative() -> None:
    seen: dict = {}

    async def connect_first(**kwargs: object) -> dict:
        seen.update(kwargs)
        return {"ok": False, "reason": "connect_required"}

    resolution = await resolve_tool_requirements(
        object(),
        tool_name="mail_search",
        operation="search",
        requirements=[MAIL_ANY_OF],
        identity={"user_id": "user-1"},
        resolution_source={},
        credential_resolver=_resolver({}),
        credential_view_resolver=view,
        connect_first_denial_builder=connect_first,
    )
    assert resolution.denial == {"ok": False, "reason": "connect_required"}
    assert [item["provider_id"] for item in seen["requirements"]] == ["google", "icloud_mail"]
    assert seen["missing"] == ["email:read", "gmail:read"]


@pytest.mark.asyncio
async def test_flat_requirements_keep_and_semantics() -> None:
    resolution = await resolve_tool_requirements(
        object(),
        tool_name="slack_to_sheets",
        operation="run",
        requirements=[
            {"provider_id": "slack", "claims": ["slack:search"]},
            {"provider_id": "google", "claims": ["sheets:write"]},
        ],
        identity={"user_id": "user-1"},
        resolution_source={},
        credential_resolver=_resolver({"slack": "slack_1"}),
        credential_view_resolver=view,
        connect_first_denial_builder=None,
    )
    assert not resolution.allowed  # google missing denies even though slack resolved


def test_provider_catalog_exposes_settings_but_never_secrets():
    from connection_hub.delegated_to_kdcube.models import IntegrationProvider

    provider = IntegrationProvider.from_config(
        "icloud_mail",
        {
            "adapter": "email.imap_smtp_app_password",
            "settings": {
                "imap_host": "imap.mail.me.com", "imap_port": 993,
                "smtp_host": "smtp.mail.me.com", "smtp_port": 587, "smtp_starttls": True,
                "client_secret": "never", "api_token": "never",
            },
        },
    )
    catalog = provider.to_dict()
    assert catalog["adapter_config"] == {
        "imap_host": "imap.mail.me.com", "imap_port": 993,
        "smtp_host": "smtp.mail.me.com", "smtp_port": 587, "smtp_starttls": True,
    }
