from __future__ import annotations

from types import SimpleNamespace

import pytest

from prokura.mcp_tool_enforcement import enforce_tool_requirements


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
