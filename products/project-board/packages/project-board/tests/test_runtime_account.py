"""Coding-runtime account identity is public metadata, never credential data."""

from __future__ import annotations

import base64
import json

import pytest

from project_board.client import relay
from project_board.client.runtime_account import read_runtime_account
from project_board.client.store import SharedFieldStore
from project_board.contract.errors import DomainError
from project_board.contract.runtime_account import runtime_account_evidence_advances

from relay_helpers import make_host


def test_runtime_account_evidence_advances_only_for_a_newer_observation():
    current = {
        "state": "reported",
        "source": "host-report",
        "observed_at": "2026-09-29T12:02:00Z",
    }

    assert runtime_account_evidence_advances(
        current,
        {
            "state": "stale",
            "source": "host-report",
            "observed_at": "2026-09-29T12:03:00Z",
        },
    )
    for observed_at in (
        "2026-09-29T12:01:00Z",
        "2026-09-29T12:02:00Z",
    ):
        assert not runtime_account_evidence_advances(
            current,
            {
                "state": "missing",
                "source": "host-report",
                "observed_at": observed_at,
            },
        )
    assert runtime_account_evidence_advances(
        {},
        {
            "state": "reported",
            "source": "host-report",
            "observed_at": "2026-09-29T12:00:00Z",
        },
    )
    assert not runtime_account_evidence_advances(current, {})


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
async def test_missing_and_unreadable_vendor_accounts_are_distinct_safe_errors(tmp_path):
    auth = tmp_path / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(json.dumps({"tokens": {"access_token": "do-not-show"}}))

    with pytest.raises(DomainError) as raised:
        await read_runtime_account("codex", home=tmp_path)

    assert raised.value.code == "work_runtime_account_missing"
    assert "do-not-show" not in str(raised.value)

    auth.write_text("{not-json", encoding="utf-8")
    with pytest.raises(DomainError) as unreadable:
        await read_runtime_account("codex", home=tmp_path)
    assert unreadable.value.code == "work_runtime_account_unavailable"


@pytest.mark.asyncio
async def test_each_relay_heartbeat_states_reported_stale_or_missing_account(tmp_path):
    host, _identity, channel = make_host(tmp_path)
    calls: list[dict] = []
    observations = [
        {"account_id": "acct-one", "email": "one@example.test", "organization": ""},
        DomainError(
            "work_runtime_account_unavailable",
            "The coding runtime account file could not be read.",
        ),
        DomainError(
            "work_runtime_account_missing",
            "The coding runtime did not report a signed-in account.",
        ),
    ]

    class Client:
        async def action(self, **kwargs):
            calls.append(dict(kwargs))
            return {"object": {}}

    async def account_reader():
        observation = observations.pop(0)
        if isinstance(observation, Exception):
            raise observation
        return observation

    adapter = relay.ProblemBoardHostRelayAdapter(
        config=relay.RelayConfig.from_host_channel(host, channel, project_id="attendance"),
        field=SharedFieldStore(host.field_root),
        client=Client(),
        runtime_account_reader=account_reader,
    )

    await adapter._heartbeat_with_republish({"availability": "available"})  # noqa: SLF001
    await adapter._heartbeat_with_republish({"availability": "available"})  # noqa: SLF001
    await adapter._heartbeat_with_republish({"availability": "available"})  # noqa: SLF001

    assert calls[0]["payload"]["runtime_account"]["account_id"] == "acct-one"
    assert calls[0]["payload"]["runtime_account_evidence"]["state"] == "reported"
    assert calls[0]["payload"]["runtime_account_evidence"]["source"] == "host-report"
    assert calls[0]["payload"]["runtime_account_evidence"]["observed_at"]
    assert "runtime_account" not in calls[1]["payload"]
    assert calls[1]["payload"]["runtime_account_evidence"]["state"] == "stale"
    assert "runtime_account" not in calls[2]["payload"]
    assert calls[2]["payload"]["runtime_account_evidence"]["state"] == "missing"
