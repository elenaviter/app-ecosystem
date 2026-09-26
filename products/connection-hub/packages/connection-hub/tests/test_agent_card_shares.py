"""An owner shares an agent's Card with a named person, at view or edit (W319, slice 2).

The share lives next to the Card in the Card store. View opens the Card
read-only; edit also changes it, under the owner's storage key with the
person recorded. An unshare takes effect at once, and the person is told why.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from connection_hub.delegated_credentials.agent_card_shares import AgentCardShares
from connection_hub.delegated_credentials.cards.identity import CARD_KIND_AGENT
from connection_hub.delegated_credentials.cards.model import CardAuthority
from connection_hub.delegated_credentials.cards.store import (
    BundleStorageDelegatedCardStore,
    subject_hash_for,
)
from connection_hub.delegated_credentials.project_agent_card_access import (
    PROJECT_AGENT_CARD_AUDIT_PROVENANCE,
    ProjectAgentCardAccess,
)

from test_project_agent_card_access import ADA, Host, Port

ACCESS = "aut_agent"
BORIS = {"user_id": "boris", "roles": ["kdcube:role:registered"], "permissions": []}
MO = {"user_id": "mo", "roles": ["kdcube:role:registered"], "permissions": []}


def _store(tmp_path, *, state="active"):
    store = BundleStorageDelegatedCardStore(tmp_path)
    authority = CardAuthority(
        access_id=ACCESS, client_id="client-1", grantor_subject="boris", delegate_subject="agent-1",
        source="agent", card_kind=CARD_KIND_AGENT, card_revision=1, created_at=1_900_000_000,
        expires_at=4_000_000_000, label="claude-ops", state=state,
    )

    async def seed():
        pointer = await store.write_revision(
            subject_hash=subject_hash_for("boris"), authority=authority,
            updated_at=datetime(2026, 9, 26, tzinfo=timezone.utc),
        )
        await store.advance_current(subject_hash=subject_hash_for("boris"), pointer=pointer)

    asyncio.run(seed())
    return store


def _run(coro):
    return asyncio.run(coro)


def test_only_the_owner_shares_lists_and_unshares(tmp_path):
    shares = AgentCardShares(_store(tmp_path))
    shared = _run(shares.share(BORIS, access_id=ACCESS, grantee_subject="ada", level="view"))
    assert shared["ok"] is True and shared["share"]["level"] == "view" and shared["share"]["label"] == "claude-ops"
    _run(shares.share(BORIS, access_id=ACCESS, grantee_subject="mo", level="edit"))
    listed = _run(shares.shares(BORIS, access_id=ACCESS))
    assert [(row["grantee_subject"], row["level"]) for row in listed["items"]] == [("ada", "view"), ("mo", "edit")]

    for call in (
        shares.share(ADA, access_id=ACCESS, grantee_subject="mo", level="edit"),
        shares.unshare(ADA, access_id=ACCESS, grantee_subject="mo"),
        shares.shares(ADA, access_id=ACCESS),
    ):
        refused = _run(call)
        assert refused["ok"] is False and refused["error"] == "agent_card_share_not_owner"


def test_bad_share_requests_are_refused_by_name(tmp_path):
    shares = AgentCardShares(_store(tmp_path))
    cases = {
        "agent_card_share_level_invalid": dict(grantee_subject="ada", level="admin"),
        "agent_card_share_grantee_invalid": dict(grantee_subject="integration:bot", level="view"),
        "agent_card_share_grantee_is_owner": dict(grantee_subject="boris", level="view"),
    }
    for error, kwargs in cases.items():
        assert _run(shares.share(BORIS, access_id=ACCESS, **kwargs))["error"] == error
    assert _run(shares.share(BORIS, access_id="../x", grantee_subject="ada", level="view"))["error"] == "agent_card_share_access_id_invalid"
    revoked_card = AgentCardShares(_store(tmp_path / "revoked", state="revoked"))
    assert _run(revoked_card.share(BORIS, access_id=ACCESS, grantee_subject="ada", level="view"))["error"] == "agent_card_share_card_not_active"


def test_shared_with_me_lists_live_shares_and_revoked_ones(tmp_path):
    shares = AgentCardShares(_store(tmp_path))
    _run(shares.share(BORIS, access_id=ACCESS, grantee_subject="ada", level="edit"))
    mine = _run(shares.shared_with_me(ADA))
    assert [(row["access_id"], row["grantor_subject"], row["level"]) for row in mine["items"]] == [(ACCESS, "boris", "edit")]
    assert mine["revoked"] == []

    unshared = _run(shares.unshare(BORIS, access_id=ACCESS, grantee_subject="ada"))
    assert unshared["removed"] is True
    after = _run(shares.shared_with_me(ADA))
    assert after["items"] == [] and [row["level"] for row in after["revoked"]] == ["revoked"]
    assert _run(shares.shares(BORIS, access_id=ACCESS))["items"] == [], "the owner's list shows live shares"
    assert _run(shares.unshare(BORIS, access_id=ACCESS, grantee_subject="ada"))["removed"] is False
    assert _run(shares.shared_with_me(MO)) == {"ok": True, "items": [], "revoked": []}


def test_a_stale_index_entry_grants_nothing(tmp_path):
    store = _store(tmp_path)
    shares = AgentCardShares(store)
    _run(shares.share(BORIS, access_id=ACCESS, grantee_subject="ada", level="edit"))
    # The Card-side record is the authority; the index only finds it.
    store.share_path(subject_hash=subject_hash_for("boris"), access_id=ACCESS,
                     grantee_hash=subject_hash_for("ada")).unlink()
    assert _run(shares.share_for("ada", ACCESS)) is None
    assert _run(shares.shared_with_me(ADA))["items"] == []


def test_view_opens_read_only_and_a_change_is_refused_with_the_reason(tmp_path):
    shares = AgentCardShares(_store(tmp_path))
    _run(shares.share(BORIS, access_id=ACCESS, grantee_subject="ada", level="view"))
    host, port = Host(), Port()
    access = ProjectAgentCardAccess(host, port, shares=shares)

    opened = _run(access.get(ADA, access_id=ACCESS, project_ref=""))
    assert opened["ok"] is True and opened["access"]["via"] == "shared_view" and opened["access"]["can_edit"] is False
    assert host.calls[0] == ("list_access", {"user_id": "boris", "roles": [], "permissions": []})

    refused = _run(access.update(ADA, access_id=ACCESS, project_ref="", resource_grants={}))
    assert refused["ok"] is False and refused["error"] == "agent_card_shared_view_only"
    assert "boris shares this agent with you to view" in refused["message"]
    assert not any(name == "update_access" for name, _ in host.calls)


def test_edit_changes_the_card_under_the_owners_key_audited(tmp_path):
    shares = AgentCardShares(_store(tmp_path))
    _run(shares.share(BORIS, access_id=ACCESS, grantee_subject="ada", level="edit"))
    host, port = Host(), Port()
    access = ProjectAgentCardAccess(host, port, shares=shares)

    opened = _run(access.get(ADA, access_id=ACCESS, project_ref=""))
    assert opened["access"]["via"] == "shared_edit" and opened["access"]["can_edit"] is True
    changed = _run(access.update(ADA, access_id=ACCESS, project_ref="", resource_grants={"problem_board": ["work:relay"]}))
    assert changed == {"ok": True, "card_revision": 4}
    name, call = host.calls[-1]
    assert name == "update_access" and call["user"] == {"user_id": "boris", "roles": [], "permissions": []}
    audit = call["transformed"].provenance[PROJECT_AGENT_CARD_AUDIT_PROVENANCE]
    assert audit["actor_subject"] == "ada" and audit["via"] == "shared_edit"
    profile = _run(access.apply_profile(ADA, access_id=ACCESS, project_ref="", profile="coordinator"))
    assert profile == {"ok": True, "profile": "coordinator"}, "Make coordinator works for an editor"
    assert port.calls == [], "a share is decided in Connection Hub, not by the project host"


def test_an_unshare_takes_effect_at_once_and_says_why(tmp_path):
    shares = AgentCardShares(_store(tmp_path))
    _run(shares.share(BORIS, access_id=ACCESS, grantee_subject="ada", level="edit"))
    access = ProjectAgentCardAccess(Host(), Port(), shares=shares)
    assert _run(access.get(ADA, access_id=ACCESS, project_ref=""))["ok"] is True

    _run(shares.unshare(BORIS, access_id=ACCESS, grantee_subject="ada"))
    for call in (access.get(ADA, access_id=ACCESS, project_ref=""),
                 access.update(ADA, access_id=ACCESS, project_ref="", resource_grants={})):
        refused = _run(call)
        assert refused["ok"] is False and refused["error"] == "agent_card_share_revoked"
        assert refused["message"] == "boris no longer shares this agent with you."


def test_a_view_share_does_not_hide_a_project_admins_edit(tmp_path):
    from test_project_agent_card_access import PROJECT, _allow

    shares = AgentCardShares(_store(tmp_path))
    _run(shares.share(BORIS, access_id=ACCESS, grantee_subject="ada", level="view"))
    host, port = Host(), Port({(PROJECT, "write"): _allow("project_admin", "write")})
    changed = _run(ProjectAgentCardAccess(host, port, shares=shares).update(
        ADA, access_id=ACCESS, project_ref=PROJECT, resource_grants={}))
    assert changed == {"ok": True, "card_revision": 4}


def test_the_host_cannot_answer_shared_edit(tmp_path):
    from connection_hub.delegated_credentials.project_agent_card_access import AgentCardDecision

    forged = AgentCardDecision(allowed=True, via="shared_edit", grantor_subject="boris", access_id=ACCESS,
                               project_ref="", action="write")
    refused = _run(ProjectAgentCardAccess(Host(), Port({("", "write"): forged})).update(
        ADA, access_id=ACCESS, project_ref="", resource_grants={}))
    assert refused["error"] == "project_agent_card_write_denied"
