# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""An expired manual card keeps its grants and is renewed in place: it stays
listed, renewal mints a new bearer on the same card and access_id, the old
session ends, and the other card families are pointed to their own way back."""

from __future__ import annotations

import dataclasses
import time

import pytest

from connection_hub.delegated_credentials.automation_access import ACCESS_SOURCE_OAUTH
from connection_hub.delegated_credentials.cards.model import CARD_STATE_REVOKED
from connection_hub.delegated_credentials.cards.service import replace_state
from test_resident_profile_cards import GRANTOR, MEMORIES, OTHER_USER, USER, _Harness


async def _manual_card(harness: _Harness, *, ttl: int = 3600) -> dict:
    created = await harness.service.create_access(
        USER,
        label="Steuer",
        resource_grants={MEMORIES: ["memories:read", "memories:write"]},
        resource_operations={MEMORIES: ["search", "write"]},
        ttl_seconds=ttl,
    )
    assert created["ok"] is True, created
    return created


def _expire(harness: _Harness, access_id: str, *, ago: int = 60) -> None:
    authority, handles = harness.persistence.cards[access_id]
    harness.persistence.cards[access_id] = (
        dataclasses.replace(authority, expires_at=int(time.time()) - ago),
        handles,
    )


@pytest.mark.asyncio
async def test_an_expired_card_stays_listed_and_says_so(tmp_path):
    harness = _Harness(tmp_path)
    created = await _manual_card(harness)
    access_id = created["access"]["access_id"]
    _expire(harness, access_id)

    listed = await harness.service.list_access(USER)
    assert listed["ok"] is True
    items = {item["access_id"]: item for item in listed["items"]}
    assert access_id in items, "an expired card must not vanish from its owner's list"
    assert items[access_id]["expired"] is True
    assert items[access_id]["resource_grants"][MEMORIES] == ["memories:read", "memories:write"]


@pytest.mark.asyncio
async def test_an_expired_card_is_revoked_by_its_owner(tmp_path):
    harness = _Harness(tmp_path)
    created = await _manual_card(harness)
    access_id = created["access"]["access_id"]
    _expire(harness, access_id)

    foreign = await harness.service.revoke_access(OTHER_USER, access_id=access_id)
    assert foreign.get("removed") is not True
    assert harness.persistence.cards[access_id][0].state != CARD_STATE_REVOKED

    revoked = await harness.service.revoke_access(USER, access_id=access_id)
    assert revoked["ok"] is True and revoked["removed"] is True, revoked
    assert harness.persistence.cards[access_id][0].state == CARD_STATE_REVOKED
    listed = await harness.service.list_access(USER)
    assert access_id not in {item["access_id"] for item in listed["items"]}

    again = await harness.service.revoke_access(USER, access_id=access_id)
    assert again == {"ok": True, "removed": False}


@pytest.mark.asyncio
async def test_renewal_keeps_the_card_and_mints_a_new_bearer(tmp_path):
    harness = _Harness(tmp_path)
    created = await _manual_card(harness, ttl=3600)
    access_id = created["access"]["access_id"]
    old_token = created["access_token"]
    old_revision = created["access"]["card_revision"]
    old_session = harness.persistence.cards[access_id][1].session_id
    _expire(harness, access_id)

    renewed = await harness.service.renew_access(USER, access_id=access_id)
    assert renewed["ok"] is True, renewed
    access = renewed["access"]
    assert access["access_id"] == access_id
    assert access["card_revision"] == old_revision + 1
    assert renewed["access_token"] and renewed["access_token"] != old_token
    assert renewed["authorization_header"] == f"Bearer {renewed['access_token']}"
    now = int(time.time())
    # Default lifetime is the card's previous one (one hour here).
    assert now + 3600 - 5 <= access["expires_at"] <= now + 3600 + 5
    assert access["last_issued_at"] >= now - 5
    assert access["last_four"] == renewed["access_token"][-4:]
    # The work survives untouched.
    assert access["resource_grants"][MEMORIES] == ["memories:read", "memories:write"]
    assert sorted(access["resource_operations"][MEMORIES]) == ["search", "write"]
    assert access["provenance"]["renewals"] == 1
    # The new bearer is bound to the same card; the old session is over.
    assert harness.grant_store.bindings[renewed["access_token"]]["registry_access_id"] == access_id
    assert old_session in harness.authority.logged_out

    listed = await harness.service.list_access(USER)
    item = next(item for item in listed["items"] if item["access_id"] == access_id)
    assert item["expired"] is False


@pytest.mark.asyncio
async def test_renewal_before_expiry_rotates_and_honours_a_requested_lifetime(tmp_path):
    harness = _Harness(tmp_path)
    created = await _manual_card(harness, ttl=3600)
    access_id = created["access"]["access_id"]

    renewed = await harness.service.renew_access(USER, access_id=access_id, ttl_seconds=7200)
    assert renewed["ok"] is True, renewed
    now = int(time.time())
    assert now + 7200 - 5 <= renewed["access"]["expires_at"] <= now + 7200 + 5
    assert renewed["access_token"] != created["access_token"]


@pytest.mark.asyncio
async def test_renewal_refusals(tmp_path):
    harness = _Harness(tmp_path)
    created = await _manual_card(harness)
    access_id = created["access"]["access_id"]

    # Another user cannot see it, let alone renew it.
    other = await harness.service.renew_access(OTHER_USER, access_id=access_id)
    assert other["ok"] is False and other["error"] == "delegated_access_not_found"

    # A connected app renews by reconnecting from the client.
    authority, handles = harness.persistence.cards[access_id]
    harness.persistence.cards[access_id] = (
        dataclasses.replace(authority, source=ACCESS_SOURCE_OAUTH), handles,
    )
    oauth = await harness.service.renew_access(USER, access_id=access_id)
    assert oauth["ok"] is False and oauth["error"] == "delegated_access_renew_unsupported"
    assert oauth["source"] == ACCESS_SOURCE_OAUTH
    harness.persistence.cards[access_id] = (authority, handles)

    # A revoked card is not revived by renewal.
    harness.persistence.cards[access_id] = (replace_state(authority, CARD_STATE_REVOKED), handles)
    revoked = await harness.service.renew_access(USER, access_id=access_id)
    assert revoked["ok"] is False and revoked["error"] == "delegated_access_revoked"

    unknown = await harness.service.renew_access(USER, access_id="missing")
    assert unknown["ok"] is False and unknown["error"] == "delegated_access_not_found"
    assert GRANTOR == USER["user_id"]


class _ProlongingGrantStore:
    """A grant store whose refresh tokens can be extended, as the real one."""

    refresh_ttl = 86400

    def __init__(self) -> None:
        self.bindings: dict[str, dict] = {}
        self.extended_refresh: list[tuple[str, int]] = []
        self.extended_grants: list[tuple[str, int]] = []
        self.extended_cards: list[tuple[str, int]] = []
        self.revoked_cards: list[str] = []
        self.refresh_alive = True

    async def bind_access_grant(self, token, operations, expires_in, **kwargs):
        self.bindings[token] = {"operations": list(operations), **kwargs}

    async def revoke_access_grant(self, token):
        self.bindings.pop(token, None)

    async def revoke_refresh_token(self, token):
        return True

    async def extend_refresh_token(self, token, ttl_seconds):
        if not self.refresh_alive:
            return False
        self.extended_refresh.append((token, int(ttl_seconds)))
        return True

    async def extend_access_grant(self, token, ttl_seconds):
        self.extended_grants.append((token, int(ttl_seconds)))
        return True

    async def extend_card_credentials(self, access_id, ttl_seconds):
        if not self.refresh_alive:
            return False
        self.extended_cards.append((access_id, int(ttl_seconds)))
        return True

    async def revoke_card_credentials(self, access_id):
        self.revoked_cards.append(access_id)
        return True


def _as_connected_app(harness: _Harness, access_id: str, *, refresh_token: str, access_token: str) -> None:
    from connection_hub.delegated_credentials.cards.model import CardCredentialHandles
    authority, _ = harness.persistence.cards[access_id]
    harness.persistence.cards[access_id] = (
        dataclasses.replace(authority, source=ACCESS_SOURCE_OAUTH),
        CardCredentialHandles(access_id=access_id, access_token=access_token, refresh_token=refresh_token),
    )


@pytest.mark.asyncio
async def test_prolonging_a_connected_app_extends_its_refresh_token_and_the_card(tmp_path):
    harness = _Harness(tmp_path)
    store = _ProlongingGrantStore()
    harness.service._store = store
    created = await _manual_card(harness, ttl=3600)
    access_id = created["access"]["access_id"]
    _as_connected_app(harness, access_id, refresh_token="rt-1", access_token="at-1")
    old_revision = harness.persistence.cards[access_id][0].card_revision

    prolonged = await harness.service.renew_access(USER, access_id=access_id, mode="prolong")
    assert prolonged["ok"] is True, prolonged
    assert prolonged["mode"] == "prolong"
    assert "access_token" not in prolonged, "prolonging never hands out a token"
    now = int(time.time())
    assert now + 3600 - 5 <= prolonged["access"]["expires_at"] <= now + 3600 + 5
    assert prolonged["access"]["card_revision"] == old_revision + 1
    assert prolonged["access"]["provenance"]["prolongations"] == 1
    assert store.extended_refresh == [("rt-1", 3600)]
    assert store.extended_grants == [("at-1", 3600)]
    # The client's handles are untouched: it keeps what it has.
    handles = harness.persistence.cards[access_id][1]
    assert (handles.refresh_token, handles.access_token) == ("rt-1", "at-1")


@pytest.mark.asyncio
async def test_prolong_and_revoke_address_durable_oauth_by_card_id(tmp_path):
    from connection_hub.delegated_credentials.cards.model import CardCredentialHandles

    harness = _Harness(tmp_path)
    store = _ProlongingGrantStore()
    harness.service._store = store
    created = await _manual_card(harness, ttl=3600)
    access_id = created["access"]["access_id"]
    authority, _handles = harness.persistence.cards[access_id]
    harness.persistence.cards[access_id] = (
        dataclasses.replace(authority, source=ACCESS_SOURCE_OAUTH),
        CardCredentialHandles(access_id=access_id),
    )

    prolonged = await harness.service.renew_access(
        USER,
        access_id=access_id,
        mode="prolong",
    )

    assert prolonged["ok"] is True, prolonged
    assert store.extended_cards == [(access_id, 3600)]
    assert store.extended_refresh == []
    assert store.extended_grants == []

    revoked = await harness.service.revoke_access(USER, access_id=access_id)

    assert revoked["ok"] is True, revoked
    assert revoked["refresh_token_revoked"] is True
    assert store.revoked_cards == [access_id]


@pytest.mark.asyncio
async def test_prolonging_refusals(tmp_path):
    harness = _Harness(tmp_path)
    store = _ProlongingGrantStore()
    harness.service._store = store
    created = await _manual_card(harness)
    access_id = created["access"]["access_id"]

    # A manual bearer carries its own end date: reissue, never prolong.
    manual = await harness.service.renew_access(USER, access_id=access_id, mode="prolong")
    assert manual["ok"] is False and manual["error"] == "delegated_access_prolong_unsupported"

    # A connected app whose refresh token already ended must reconnect.
    _as_connected_app(harness, access_id, refresh_token="rt-gone", access_token="at-1")
    store.refresh_alive = False
    ended = await harness.service.renew_access(USER, access_id=access_id, mode="prolong")
    assert ended["ok"] is False and ended["error"] == "delegated_access_credential_expired"

    unknown_mode = await harness.service.renew_access(USER, access_id=access_id, mode="forever")
    assert unknown_mode["ok"] is False and unknown_mode["error"] == "invalid_renew_mode"


@pytest.mark.asyncio
async def test_prolonging_a_card_with_an_unresolved_transaction_extends_nothing(tmp_path):
    # W580 finding 1: the fenced precondition read comes BEFORE the credential
    # extension, and its refusal is a structured answer, not an exception.
    from connection_hub.delegated_credentials.cards.service import CardConflict
    harness = _Harness(tmp_path)
    store = _ProlongingGrantStore()
    harness.service._store = store
    created = await _manual_card(harness, ttl=3600)
    access_id = created["access"]["access_id"]
    _as_connected_app(harness, access_id, refresh_token="rt-1", access_token="at-1")
    before = harness.persistence.cards[access_id][0]

    async def staged(*args, **kwargs):
        raise CardConflict("card_transaction_unresolved")

    harness.persistence.current_revision = staged
    refused = await harness.service.renew_access(USER, access_id=access_id, mode="prolong")
    assert refused["ok"] is False and refused["error"] == "delegated_card_not_committed"
    assert refused["reason"] == "card_transaction_unresolved" and refused["retryable"] is True
    assert store.extended_refresh == [] and store.extended_grants == []
    assert harness.persistence.cards[access_id][0] == before


@pytest.mark.asyncio
async def test_prune_reports_a_card_it_could_not_prune_instead_of_skipping_it(tmp_path):
    # W580 finding 2: a binding left on a Card revives on reconnect, so a Card
    # the prune could not change (here: an unresolved transaction) is named.
    from connection_hub.delegated_credentials.cards.service import CardConflict
    harness = _Harness(tmp_path)
    created = await _manual_card(harness, ttl=3600)
    access_id = created["access"]["access_id"]
    record = (await harness.service._list_active_records(USER["user_id"]))[0]
    bound = dataclasses.replace(record, account_scope={"google": {"acct-1": ("mail",)}})

    async def records(subject):
        return [bound]

    async def staged(*args, **kwargs):
        raise CardConflict("card_transaction_unresolved")

    harness.service._list_active_records = records
    harness.service._persist_record = staged
    outcome = await harness.service.prune_account_from_grants(
        grantor_subject=USER["user_id"], provider_id="google", account_id="acct-1")
    assert outcome["ok"] is False and outcome["not_pruned"] == [access_id]
    assert outcome["reason"] == "account_binding_not_pruned" and outcome["retryable"] is True
    assert outcome["pruned"] == 0


@pytest.mark.asyncio
async def test_prune_of_an_unstaged_card_still_succeeds(tmp_path):
    harness = _Harness(tmp_path)
    await _manual_card(harness, ttl=3600)
    record = (await harness.service._list_active_records(USER["user_id"]))[0]
    bound = dataclasses.replace(record, account_scope={"google": {"acct-1": ("mail",)}})
    written = []

    async def records(subject):
        return [bound]

    async def persist(rec, *, expected_revision, **kwargs):
        written.append((rec.account_scope, expected_revision))

    harness.service._list_active_records = records
    harness.service._persist_record = persist
    outcome = await harness.service.prune_account_from_grants(
        grantor_subject=USER["user_id"], provider_id="google", account_id="acct-1")
    assert outcome == {"ok": True, "pruned": 1, "grants": [record.access_id], "not_pruned": []}
    assert written == [({}, record.card_revision)]


def _bind(harness, access_id):
    from connection_hub.delegated_credentials.cards.model import ControlCardBinding

    persistence = harness.persistence

    async def persist_guarded(authority, handles, *, subject_hash, expected_revision, before_commit):
        await before_commit()  # production calls it inside the target lock, after its revision check
        await persistence.persist(authority, handles, subject_hash=subject_hash, expected_revision=expected_revision)

    persistence.persist_guarded = persist_guarded
    authority, handles = harness.persistence.cards[access_id]
    harness.persistence.cards[access_id] = (
        dataclasses.replace(authority, control_card=ControlCardBinding(
            control_id="control-1", issuer_ref="work:project:one", issuer_kind="project", control_revision=1)),
        handles,
    )


class _Policy:
    def __init__(self, allow):
        self.allow, self.calls = allow, []

    def _decision(self, request):
        from datetime import datetime, timedelta, timezone
        from connection_hub.delegated_credentials.caller_writer_gate import CallerWriteDecision
        return CallerWriteDecision(self.allow, "" if self.allow else "pb_refused", "policy:v1",
                                   datetime.now(timezone.utc) + timedelta(minutes=5), request)

    async def decide(self, request):
        self.calls.append(("decide", request.action))
        return self._decision(request)

    async def revalidate(self, request, initial):
        self.calls.append(("revalidate", request.action))
        return self._decision(request)

    async def finalize(self, request, *, state, card_revision):
        self.calls.append(("finalize", state))
        return True


def _registry(policy):
    from connection_hub.delegated_credentials.caller_writer_gate import CallerWriterRegistry
    registry = CallerWriterRegistry()
    registry.register("project", policy)
    return registry


@pytest.mark.asyncio
@pytest.mark.parametrize("allow", [True, False])
async def test_prolonging_a_bound_card_is_decided_by_its_binding_policy(tmp_path, allow):
    # W580/B: prolongation changes the duration of authority, so it is governed.
    harness = _Harness(tmp_path)
    harness.service._store = _ProlongingGrantStore()
    created = await _manual_card(harness, ttl=3600)
    access_id = created["access"]["access_id"]
    _as_connected_app(harness, access_id, refresh_token="rt-1", access_token="at-1")
    _bind(harness, access_id)
    before = harness.persistence.cards[access_id][0]
    policy = _Policy(allow)
    harness.service.bind_caller_writers(_registry(policy))
    outcome = await harness.service.renew_access(USER, access_id=access_id, mode="prolong")
    assert [c for c in policy.calls if c[0] != "finalize"] == [("decide", "prolong"), ("revalidate", "prolong")][: 2 if allow else 1]
    assert ("finalize", "committed" if allow else "refused") in policy.calls
    after = harness.persistence.cards[access_id][0]
    if allow:
        assert outcome["ok"] is True and after.card_revision == before.card_revision + 1
    else:
        assert outcome == {"ok": False, "status": 403, "error": "pb_refused", "caller_write_outcome_confirmed": True}
        assert after == before


@pytest.mark.asyncio
async def test_prolonging_an_unbound_card_asks_no_policy(tmp_path):
    harness = _Harness(tmp_path)
    harness.service._store = _ProlongingGrantStore()
    created = await _manual_card(harness, ttl=3600)
    access_id = created["access"]["access_id"]
    _as_connected_app(harness, access_id, refresh_token="rt-1", access_token="at-1")
    policy = _Policy(False)
    harness.service.bind_caller_writers(_registry(policy))
    assert (await harness.service.renew_access(USER, access_id=access_id, mode="prolong"))["ok"] is True
    assert policy.calls == []


# ── Ops B2/B3 (12:14): the single write paths themselves, not a stub ──────


async def _bound_card(tmp_path, *, allow=True):
    harness = _Harness(tmp_path)
    created = await _manual_card(harness, ttl=3600)
    access_id = created["access"]["access_id"]
    _bind(harness, access_id)
    policy = _Policy(allow)
    harness.service.bind_caller_writers(_registry(policy))
    return harness, access_id, policy


@pytest.mark.asyncio
async def test_persist_refuses_an_unnamed_write_of_a_governed_card_before_any_effect(tmp_path):
    from connection_hub.delegated_credentials.automation_access import record_from_card
    from connection_hub.delegated_credentials.caller_writer_gate import CallerWrite, CallerWriteRefused
    harness, access_id, policy = await _bound_card(tmp_path)
    before = harness.persistence.cards[access_id][0]
    writes = harness.persistence.persist_calls
    candidate = record_from_card(dataclasses.replace(before, card_revision=before.card_revision + 1,
                                                     label="unnamed edit"))
    with pytest.raises(CallerWriteRefused, match="caller_writer_not_enlisted"):
        await harness.service._persist_record(candidate, expected_revision=before.card_revision)
    assert harness.persistence.persist_calls == writes and harness.persistence.cards[access_id][0] == before
    assert policy.calls == []
    # Named, the binding's policy decides, the write commits and its outcome is finalized.
    await harness.service._persist_record(candidate, expected_revision=before.card_revision,
                                          caller_write=CallerWrite("extend", "platform-user-1"))
    assert harness.persistence.cards[access_id][0].label == "unnamed edit"
    assert policy.calls == [("decide", "extend"), ("revalidate", "extend"), ("finalize", "committed")]


@pytest.mark.asyncio
async def test_forget_refuses_an_unnamed_revoke_of_a_governed_card(tmp_path):
    from connection_hub.delegated_credentials.automation_access import record_from_card
    from connection_hub.delegated_credentials.caller_writer_gate import CallerWriteRefused
    harness, access_id, policy = await _bound_card(tmp_path)
    before = harness.persistence.cards[access_id][0]
    with pytest.raises(CallerWriteRefused, match="caller_writer_not_enlisted"):
        await harness.service._forget_record(record_from_card(before))
    assert harness.persistence.cards[access_id][0] == before and policy.calls == []


@pytest.mark.asyncio
async def test_an_unbound_card_is_written_and_revoked_as_before(tmp_path):
    from connection_hub.delegated_credentials.automation_access import record_from_card
    harness = _Harness(tmp_path)
    created = await _manual_card(harness, ttl=3600)
    access_id = created["access"]["access_id"]
    policy = _Policy(False)
    harness.service.bind_caller_writers(_registry(policy))
    before = harness.persistence.cards[access_id][0]
    await harness.service._persist_record(
        record_from_card(dataclasses.replace(before, card_revision=before.card_revision + 1, label="x")),
        expected_revision=before.card_revision)
    await harness.service._forget_record(record_from_card(harness.persistence.cards[access_id][0]))
    assert harness.persistence.cards[access_id][0].state == CARD_STATE_REVOKED and policy.calls == []
