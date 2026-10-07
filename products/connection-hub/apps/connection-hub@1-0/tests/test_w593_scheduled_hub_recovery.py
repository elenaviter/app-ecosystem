"""W593 / W578 item 5: the Hub's card-transaction-recover cron through the REAL scheduler job path.

Run on the SDK that gives a scheduled job its own bundle-operation caller
(kdcube PR 327). What is real and what is a fixture:

REAL
- ``bundle_scheduler._invoke_job`` with ``bind_bundle_job_context``, the
  normal ``make_local_bundle_operation_caller``, the real session constructor
  and the local dispatch to the peer bundle's public operation;
- ``ConnectionHubEntrypoint.recover_card_transactions_cron`` and its whole
  composition: the PostgreSQL decision store (throwaway server), the
  coordinator, ``recover_card_transactions``, the Hub participant's finish,
  the Card store and ``RoutedDecisionPort`` with the configured caller's
  authority (``_card_participant_callers`` -> ``configured_authority_fetch``
  -> ``call_bundle_operation(route="public")``);
- the v2 authority verification of the peer's signed response.

FIXTURE (named)
- registry, bundle config and loader (as in the SDK's own scheduler tests);
- ``problem-board@1-0`` is a fixture bundle whose public
  ``project_card_transaction_authority`` signs a v2 decision for one
  transaction; it is NOT Problem Board's production authority;
- composition helpers outside this path: the Card persistence (a real file
  store and service, Redis-free), the delegated authority config flag, the
  OAuth grant store, invocation policies and catalog (unused here);
- secrets resolve from test values by reference; nothing is printed.

The recovery walk finishes the Hub's OWN in-doubt transaction (no peer call);
the Hub pulls a peer's decision only when its Card store reads a Card the
peer staged, so the second job reads such a Card through the cron-bound
store inside the same scheduled job (EApp correction, 00:01).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

PACKAGE_TESTS = Path(__file__).resolve().parents[3] / "packages" / "connection-hub" / "tests"
if str(PACKAGE_TESTS) not in sys.path:
    sys.path.insert(0, str(PACKAGE_TESTS))

HUB, PEER = "connection-hub@1-0", "problem-board@1-0"
SCOPE = "work:project:qualified"
AUTHORITY_SECRET = "a" * 40   # the peer authority's response key (fixture)
SECRETS = {"hub/request": "q" * 40, "hub/receipt": "r" * 40, "pb/authority": AUTHORITY_SECRET,
           "hub/authority-request": "s" * 40}


def _entrypoint_module():
    from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import load_dynamic_module_for_path
    _name, module = load_dynamic_module_for_path(Path(__file__).resolve().parents[1] / "entrypoint.py")
    return module


def _connections():
    return {"card_transactions": {"enabled": True, "callers": {PEER: {
        "request_secret_ref": "hub/request", "receipt_secret_ref": "hub/receipt",
        "receipt_signer_id": HUB, "audience": PEER, "hub_resource": HUB, "scope_field": "project_ref",
        "authority": {"service_id": PEER, "audience": HUB, "secret_ref": "pb/authority",
                      "request_signer_id": HUB, "request_secret_ref": "hub/authority-request",
                      "binding": {"bundle_id": PEER, "operation": "project_card_transaction_authority",
                                  "response_key": "authority", "refusals": {}, "request_fields": {}}},
    }}}}


class _PeerAuthority:
    """The fixture problem-board@1-0's decision record for its one transaction, signed per request."""

    def __init__(self, participant_input, candidate_value, *, decision):
        from service_foundation.coordination.durable_decision_log import DecisionRecord, IntentDraft

        from connection_hub.delegated_credentials.cards.card_participant import PARTICIPANT
        draft = IntentDraft(replay_scope="pb:w593", request_id="pb-w593", expires_at=int(time.time()) + 3600,
                            participants=(PARTICIPANT,),
                            payload={"project_ref": SCOPE, "participant_inputs": {PARTICIPANT: participant_input},
                                     "participant_candidates": {PARTICIPANT: candidate_value}})
        self.record = DecisionRecord(draft.bind("9" * 64, 1), "preparing", {}, {})
        self.decision = decision
        self.calls = []

    def signed(self, phase, request_echo):
        from service_foundation.coordination.durable_wire import participant_projection

        from connection_hub.delegated_credentials.cards.card_participant import PARTICIPANT
        from connection_hub.delegated_credentials.cards.transaction_authority_v2 import (
            PROTOCOL, card_authority_signature,
        )
        decided = phase == "decision"
        now = str(int(time.time()))
        unsigned = {
            "schema": PROTOCOL, "phase": phase, "request_echo": request_echo, "audience": HUB,
            "participant": PARTICIPANT, "global_intent_bytes": self.record.intent.canonical_bytes.decode("utf-8"),
            "global_intent_digest": self.record.intent.digest,
            "projection": participant_projection(self.record.intent, PARTICIPANT),
            "candidate": self.record.intent.as_mapping()["payload"]["participant_candidates"][PARTICIPANT],
            "decision": self.decision if decided else "undecided", "decided_at": int(time.time()) if decided else None,
        }
        return {**unsigned, "authority_proof": {
            "service_id": PEER, "timestamp": now,
            "signature": card_authority_signature(unsigned, secret=AUTHORITY_SECRET, service_id=PEER,
                                                  timestamp=now)}}

    async def fetch(self, transaction_id, phase, request_echo):
        """In-process fetch, used ONLY to stage the peer's Card in setup (the prepare the peer drove)."""
        from connection_hub.delegated_credentials.cards.transaction_authority_v2 import TransactionAuthorityRefused
        if phase == "decision" and self.decision is None:
            raise TransactionAuthorityRefused("authority_decision_pending")
        return self.signed(phase, request_echo)

    def instance(self):
        """The fixture bundle problem-board@1-0, served through the REAL local dispatch."""
        from kdcube_ai_app.infra.plugin.bundle_loader import api
        outer = self

        class Peer:
            @api(alias="project_card_transaction_authority", route="public")
            async def project_card_transaction_authority(self, data=None, user_id=None, fingerprint=None,
                                                         **kwargs):
                from kdcube_ai_app.apps.chat.sdk.infra.auth_context import get_current_auth_context
                from kdcube_ai_app.apps.chat.sdk.runtime.comm_ctx import get_current_request_context

                context, auth = get_current_request_context(), get_current_auth_context()
                payload = dict(data or kwargs)
                outer.calls.append({
                    "phase": payload.get("phase"), "transaction_id": payload.get("transaction_id"),
                    "project_ref": payload.get("project_ref"), "transport_user_type": context.user.user_type,
                    "transport_user_id": context.user.user_id, "auth_principal": auth.principal_id,
                    "auth_user_type": auth.user_type, "proof_service": (payload.get("service_proof") or {}).get(
                        "service_id"), "user_id": user_id})
                if outer.decision is None and payload.get("phase") == "decision":
                    return {"ok": False, "error": {"code": "authority_decision_pending"}}
                return {"ok": True, "authority": outer.signed(payload["phase"], payload["request_echo"])}

        return Peer()


async def _world(tmp_path, monkeypatch, *, peer_decision="committed"):
    """The Hub entrypoint over the throwaway PostgreSQL store; one Hub-local transaction a crash left
    decided-but-unfinished; one Card the peer staged (prepared, through the real Hub participant)."""
    import asyncpg
    import uuid
    from dataclasses import replace

    from connection_hub.delegated_credentials.cards import composition
    from connection_hub.delegated_credentials.cards.authority_intent_source import (
        AuthorityCardIntentSource, AuthorityDecisionReader, CardAuthorityBinding,
    )
    from connection_hub.delegated_credentials.cards.card_participant import (
        HubCardParticipant, candidate_value, hub_participant_input,
    )
    from test_card_service import SUBJECT_HASH
    from test_card_transaction_composition import _staged
    from test_card_transaction_store import _setup

    dsn = os.environ.get("CONNECTION_HUB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("CONNECTION_HUB_TEST_POSTGRES_DSN is not set")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    tenant = f"w593{uuid.uuid4().hex[:8]}"
    decisions = await composition.postgres_decision_store(pool, tenant=tenant, project="hub")
    store, card_service, before, after = await _setup(tmp_path)
    persistence = SimpleNamespace(card_store=store, card_service=card_service)

    # The peer's Card: committed at r1, staged by the peer's transaction to r2 (scope SCOPE).
    peer_before = replace(before, access_id="aut_peer_staged", card_revision=1, label="peer original")
    await card_service.commit(peer_before, subject_hash=SUBJECT_HASH, expected_revision=0, now=int(time.time()))
    peer_after = replace(peer_before, card_revision=2, label="peer committed change")
    authority = _PeerAuthority(
        hub_participant_input(original=peer_before, candidate=peer_after, subject_hash=SUBJECT_HASH,
                              action="update", actor_subject="user:pb-admin", actor_kind="caller"),
        candidate_value(original=peer_before, candidate=peer_after), decision=peer_decision)
    binding = CardAuthorityBinding(secret=AUTHORITY_SECRET, service_id=PEER, audience=HUB)
    reader = AuthorityDecisionReader(fetch=authority.fetch, authority=binding)
    peer_participant = HubCardParticipant(
        service=card_service, store=store, decisions=reader,
        intents=AuthorityCardIntentSource(store=store, fetch=authority.fetch, authority=binding,
                                          authority_id=PEER, scope_field="project_ref"))
    await peer_participant.prepare(authority.record.intent.transaction_id)

    # The Hub's own transaction: prepared and decided COMMITTED, then the crash before finish.
    coordinator, local_tx = await _staged(composition, decisions, persistence, store, before, after,
                                          request_id="w593-crash-after-decide")
    await coordinator.decide(local_tx, "committed", witness_digest="c" * 64)

    module = _entrypoint_module()
    entry = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    entry.bundle_props = {"connections": _connections()}
    entry.pg_pool = pool
    entry.redis = AsyncMock()
    entry.redis.get = AsyncMock(return_value=None)
    entry.redis.set = AsyncMock(return_value=True)
    entry._card_decision_store = decisions  # the real PostgreSQL store the crash left the record in
    entry.config = SimpleNamespace(ai_bundle_spec=SimpleNamespace(id=HUB))
    entry.bundle_storage_root = lambda: tmp_path / "hub-storage"
    entry.runtime_identity = lambda: {"tenant": tenant, "project": "hub"}

    async def secret(entrypoint, *, secret_path, trace_scope="", warn_missing=True):
        return SECRETS.get(secret_path, "")

    async def card_persistence(entrypoint, redis):
        return persistence

    async def no_grants(entrypoint):
        return None

    monkeypatch.setattr(module, "_bundle_secret_value", secret)
    monkeypatch.setattr(module, "_delegated_authority_config", lambda entrypoint: SimpleNamespace(
        uses_postgresql=True))
    monkeypatch.setattr(module, "_delegated_card_persistence", card_persistence)
    monkeypatch.setattr(module, "_oauth_grant_store", no_grants)
    monkeypatch.setattr(module, "_invocation_policy_service", lambda entrypoint: None)
    monkeypatch.setattr(module, "_delegated_catalog_store", lambda entrypoint: None)

    async def drop():
        async with pool.acquire() as connection:
            await connection.execute(
                f"DELETE FROM {composition.DECISION_SCHEMA}.service_foundation_decisions WHERE namespace=$1",
                composition.decision_namespace(tenant=tenant, project="hub"))
        await pool.close()

    return SimpleNamespace(module=module, entry=entry, store=store, before=before, after=after, local_tx=local_tx,
                           peer_before=peer_before, peer_after=peer_after, authority=authority, pool=pool,
                           drop=drop, subject_hash=SUBJECT_HASH, decisions=decisions)


def _sdk_fixtures(monkeypatch, world):
    """Registry/config/loader fixtures exactly as the SDK's own scheduler tests; caller and dispatch are real."""
    from kdcube_ai_app.apps.chat.ingress import resolvers
    from kdcube_ai_app.infra.plugin import bundle_loader, bundle_store
    from kdcube_ai_app.infra.service_hub import inventory

    monkeypatch.setattr(resolvers, "get_pg_pool", AsyncMock(return_value=world.pool))
    monkeypatch.setattr(bundle_store, "resolve_bundle_spec_from_store", AsyncMock(return_value=SimpleNamespace(
        id=PEER, path="fixture-problem-board", module="entrypoint", singleton=False)))
    monkeypatch.setattr(bundle_store, "get_bundle_props", AsyncMock(return_value={}))

    async def resolve_secrets(request, **kwargs):
        return request

    monkeypatch.setattr(inventory, "resolve_config_request_secrets", resolve_secrets)
    monkeypatch.setattr(inventory, "create_workflow_config", lambda request: SimpleNamespace())
    peer = world.authority.instance()

    async def load(spec, config, **kwargs):
        assert kwargs.get("pg_pool") is world.pool
        return (world.entry, None) if spec.id == HUB else (peer, None)

    monkeypatch.setattr(bundle_loader, "get_workflow_instance_async", load)


async def _run_job(method_name):
    from kdcube_ai_app.apps.chat.sdk.runtime import bundle_scheduler as scheduler
    await scheduler._invoke_job(
        bundle_id=HUB, job_alias="card-transaction-recover", method_name=method_name,
        bundle_spec=SimpleNamespace(id=HUB),
        bundle_config=SimpleNamespace(tenant="w593", project="hub", redis=object()))


async def _current(world, card):
    found = await world.store.read_current_authority(subject_hash=world.subject_hash, access_id=card.access_id)
    return None if found is None else found[1]


@pytest.mark.asyncio
async def test_the_scheduled_recover_cron_finishes_the_hubs_own_transaction_without_a_peer_call(
        tmp_path, monkeypatch):
    world = await _world(tmp_path, monkeypatch)
    try:
        _sdk_fixtures(monkeypatch, world)
        seen = {}

        async def recover():
            seen["report"] = await world.entry.recover_card_transactions_cron()

        world.entry.recover = recover
        await _run_job("recover")
        assert seen["report"]["enabled"] is True and seen["report"]["finished"] >= 1, seen
        assert await _current(world, world.before) == world.after
        assert world.authority.calls == []  # the recovery walk is local: no peer call
    finally:
        await world.drop()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["committed", "aborted"])
async def test_a_scheduled_job_pulls_the_peer_decision_through_the_real_bridge(tmp_path, monkeypatch, decision):
    """Inside ONE scheduled job: the real cron composes the store, then a read of the peer-staged Card
    routes through RoutedDecisionPort -> configured authority -> the job's bound caller -> the peer."""
    world = await _world(tmp_path, monkeypatch, peer_decision=decision)
    try:
        _sdk_fixtures(monkeypatch, world)
        seen = {}

        async def recover_then_read():
            seen["report"] = await world.entry.recover_card_transactions_cron()
            seen["peer_card"] = await _current(world, world.peer_before)

        world.entry.recover_then_read = recover_then_read
        await _run_job("recover_then_read")
        expected = world.peer_after if decision == "committed" else world.peer_before
        assert seen.get("peer_card") == expected, seen
        calls = [call for call in world.authority.calls if call["phase"] == "decision"]
        assert calls and calls[0]["transaction_id"] == world.authority.record.intent.transaction_id
        assert calls[0]["project_ref"] == SCOPE and calls[0]["proof_service"] == HUB
        # The job's own service principal; the transport projection carries no interactive identity.
        assert calls[0]["auth_user_type"] == "service" and calls[0]["transport_user_type"] == "anonymous"
        assert calls[0]["transport_user_id"] is None and calls[0]["user_id"] is None
    finally:
        await world.drop()


@pytest.mark.asyncio
async def test_a_pending_peer_decision_stays_unreadable(tmp_path, monkeypatch):
    world = await _world(tmp_path, monkeypatch, peer_decision=None)
    try:
        _sdk_fixtures(monkeypatch, world)
        seen = {}

        async def recover_then_read():
            await world.entry.recover_card_transactions_cron()
            try:
                seen["peer_card"] = await _current(world, world.peer_before)
            except Exception as exc:  # noqa: BLE001 - the expected fail-closed refusal
                seen["error"] = str(exc)

        world.entry.recover_then_read = recover_then_read
        await _run_job("recover_then_read")
        assert seen == {"error": "card_transaction_undecided"}
        assert any(call["phase"] == "decision" for call in world.authority.calls)
    finally:
        await world.drop()


@pytest.mark.asyncio
async def test_outside_an_active_job_the_pull_has_no_caller_and_fails_closed(tmp_path, monkeypatch):
    world = await _world(tmp_path, monkeypatch)
    try:
        _sdk_fixtures(monkeypatch, world)
        seen = {}

        async def recover():
            seen["report"] = await world.entry.recover_card_transactions_cron()

        world.entry.recover = recover
        await _run_job("recover")  # composes and binds the store's routed decisions inside the job
        from connection_hub.delegated_credentials.cards.store import CardStorageError
        with pytest.raises(CardStorageError, match="card_transaction_undecided"):
            await _current(world, world.peer_before)  # after the job: its caller has expired
        assert world.authority.calls == []  # no peer was reached without the job's caller
    finally:
        await world.drop()
