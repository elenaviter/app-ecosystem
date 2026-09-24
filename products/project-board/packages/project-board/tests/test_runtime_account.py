"""Coding-runtime account identity is public metadata, never credential data."""

from __future__ import annotations

import base64
import json

import pytest

from project_board.client import relay
from project_board.client.runtime_account import read_runtime_account
from project_board.client.store import SharedFieldStore
from project_board.contract.errors import DomainError

from relay_helpers import make_host


def _jwt(claims: dict) -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps(claims, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"header.{encoded}.signature"


@pytest.mark.asyncio
async def test_codex_account_uses_public_claims_without_returning_tokens(tmp_path):
    auth = tmp_path / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(
        json.dumps(
            {
                "tokens": {
                    "account_id": "acct-codex",
                    "id_token": _jwt(
                        {
                            "email": "developer@example.test",
                            "https://api.openai.com/auth": {
                                "chatgpt_account_id": "claim-account",
                                "chatgpt_organization_id": "org-codex",
                            },
                        }
                    ),
                    "access_token": "private-access-token",
                    "refresh_token": "private-refresh-token",
                }
            }
        ),
        encoding="utf-8",
    )

    account = await read_runtime_account("codex", home=tmp_path)

    assert account == {
        "account_id": "acct-codex",
        "email": "developer@example.test",
        "organization": "org-codex",
    }
    encoded = json.dumps(account)
    assert "private-access-token" not in encoded
    assert "private-refresh-token" not in encoded
    assert "id_token" not in encoded


@pytest.mark.asyncio
async def test_claude_account_comes_from_oauth_account_metadata(tmp_path):
    (tmp_path / ".claude.json").write_text(
        json.dumps(
            {
                "oauthAccount": {
                    "accountUuid": "acct-claude",
                    "emailAddress": "builder@example.test",
                    "organizationUuid": "org-claude",
                },
                "oauthToken": "private-token",
            }
        ),
        encoding="utf-8",
    )

    assert await read_runtime_account("claude-code", home=tmp_path) == {
        "account_id": "acct-claude",
        "email": "builder@example.test",
        "organization": "org-claude",
    }


@pytest.mark.asyncio
async def test_missing_vendor_account_is_a_safe_error(tmp_path):
    auth = tmp_path / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(json.dumps({"tokens": {"access_token": "do-not-show"}}))

    with pytest.raises(DomainError) as raised:
        await read_runtime_account("codex", home=tmp_path)

    assert raised.value.code == "work_runtime_account_unavailable"
    assert "do-not-show" not in str(raised.value)


@pytest.mark.asyncio
async def test_each_relay_heartbeat_reads_and_sends_the_current_account(tmp_path):
    host, _identity, channel = make_host(tmp_path)
    calls: list[dict] = []
    accounts = [
        {"account_id": "acct-one", "email": "one@example.test", "organization": ""},
        {"account_id": "acct-two", "email": "two@example.test", "organization": ""},
    ]

    class Client:
        async def action(self, **kwargs):
            calls.append(dict(kwargs))
            return {"object": {}}

    async def account_reader():
        return accounts.pop(0)

    adapter = relay.ProblemBoardHostRelayAdapter(
        config=relay.RelayConfig.from_host_channel(host, channel, project_id="attendance"),
        field=SharedFieldStore(host.field_root),
        client=Client(),
        runtime_account_reader=account_reader,
    )

    await adapter._heartbeat_with_republish({"availability": "available"})  # noqa: SLF001
    await adapter._heartbeat_with_republish({"availability": "available"})  # noqa: SLF001

    assert calls[0]["payload"]["runtime_account"]["account_id"] == "acct-one"
    assert calls[1]["payload"]["runtime_account"]["account_id"] == "acct-two"
