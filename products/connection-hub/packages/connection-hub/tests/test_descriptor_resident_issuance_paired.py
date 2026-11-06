"""Paired resident lifecycle: real PG decisions/sessions/handles, files and Redis.

Run on a qualified paired Hub/SDK source runner with both test DSNs and REDIS_URL.
The shared W603 harness supplies real durable stores and Redis, with test
ports for projection and local mutation/request locks. The SDK authority uses
its real PostgreSQL session store with a test cache and synthetic signing key.
Custody uses the SDK's file secrets manager across fresh compositions. These
cases qualify durable lifecycle/expiry cuts; the broad gate also owes the
production lock, cache and complete admission coverage.
"""

from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from connection_hub.delegated_credentials.application_resources import application_resource
from connection_hub.delegated_credentials.automation_access import record_from_card
from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.card_participant import PARTICIPANT
from connection_hub.delegated_credentials.cards.effect_targets import compose_card_effects
from connection_hub.delegated_credentials.cards.store import CardStorageError, subject_hash_for
from connection_hub.delegated_credentials.catalog.reservations import CatalogReservations
from connection_hub.delegated_credentials.catalog.store import BundleStorageDelegatedCatalogStore
from connection_hub.delegated_credentials.descriptor_resident_issuance import (
    ResidentIssuancePending, begin_descriptor_resident_issuance, issue_descriptor_resident,
    recover_resident_originals,
)
from test_agent_capability_control_sync import AGENT, APPLICATION, OWNER, _service
from test_descriptor_agent_consent import _sync_arguments
from test_w603_original_issuance import _world

USER = {"user_id": OWNER, "roles": ["kdcube:role:super-admin"]}


@asynccontextmanager
async def _paired(tmp_path):
    pytest.importorskip("kdcube_ai_app", reason="the paired SDK source must be installed on the test worker")
    from kdcube_ai_app.auth.bundle.session_store import PostgresBundleSessionStore
    from kdcube_ai_app.infra.secrets.manager import SecretsFileSecretsManager, SecretsManagerConfig
    from kdcube_ai_app.infra.secrets.ephemeral import KDCubeEphemeralSecretStore
    from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.cards.credential_handles import (
        postgres_card_credential_handle_store,
    )
    from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.cards.resident_issuance import (
        ResidentCredentialProvider,
    )
    from kdcube_ai_app.auth.tests.test_bound_session_issuer import authority

    async with _world(tmp_path / "cards", postgres_handles=True) as w:
        w.subject_hash = subject_hash_for(OWNER)
        w.sessions = PostgresBundleSessionStore(pg_pool=w.pool, tenant=w.authority.tenant, project=w.authority.project)
        await w.sessions.ensure_schema()

        async def reader(*, tenant, project, session_id):
            assert (tenant, project) == (w.sessions.tenant, w.sessions.project)
            state = await w.sessions.get_validation_state(session_id)
            return state.session if state else None

        def recompose():
            manager = SecretsFileSecretsManager(SecretsManagerConfig(
                provider="secrets-file", component="paired-test", tenant=w.authority.tenant,
                project=w.authority.project, global_secrets_yaml=str(tmp_path / "secrets.yaml"),
                runtime_secrets_root=str(tmp_path / "custody"), runtime_secret_namespaces=("card-credentials",)))
            w.secrets = KDCubeEphemeralSecretStore(manager, namespace="card-credentials", bundle_id="connection-hub@1-0")
            w.handles = postgres_card_credential_handle_store(pg_pool=w.pool, tenant=w.authority.tenant,
                project=w.authority.project, secret_store=w.secrets)
            w.provider = ResidentCredentialProvider(authority_factory=lambda **kwargs: authority(w.sessions),
                                                    session_state_reader=reader)
            w.service._persistence._handles = w.handles
            w.service.bind_card_credential_handles(w.handles)
            w.service.bind_resident_credentials(w.provider)
            compose_card_effects(card_service=w.cards, card_store=w.store, grant_store=w.service._store,
                policies=None, issuance_store=w.authority, credential_handles=w.handles,
                decisions=w.decisions, resident_credentials=w.provider)

        # Build valid descriptor input through the public OFF path, on test
        # persistence only. No such fake bearer enters the productive stores.
        seed, records = _service(named_services=True)
        seed._tenant, seed._project = w.authority.tenant, w.authority.project
        w.arguments = _sync_arguments()
        w.arguments["capability_authority"] = {**w.arguments["capability_authority"], "resource": application_resource(
            tenant=w.authority.tenant, project=w.authority.project, application=APPLICATION, agent=AGENT)}
        answer = await seed.sync_agent_capability_control({"user_id": OWNER}, **w.arguments)
        assert answer["ok"], answer
        # The seed persistence splits authority and handles; lifecycle callers
        # take their combined record, including the credential fields below.
        w.resident = record_from_card(*records.records[answer["card"]["access_id"]])
        w.resident = replace(w.resident, access_token="", session_id="", last_four="")
        w.control = record_from_card(*records.records[answer["control_card"]["access_id"]])
        w.service._config, w.service._catalog_resolver = seed._config, seed._catalog_resolver
        w.service._minter = AsyncMock(side_effect=AssertionError("transactions ON must not call the active minter"))
        catalog_store = BundleStorageDelegatedCatalogStore(tmp_path / "catalog")
        w.catalog_store = catalog_store
        w.catalog = await w.service._active_catalog()
        await catalog_store.write_version(w.catalog)
        await catalog_store.publish_active(w.catalog)
        tx.bind_catalog_reservations(w.store, CatalogReservations(catalog_store))
        w.recompose = recompose
        recompose()
        try:
            yield w
        finally:
            async with w.pool.acquire() as connection:
                await connection.execute(f"DROP SCHEMA IF EXISTS {w.sessions.schema} CASCADE")


def _request(w, *, original=None, resident=None, original_control=None, control=None, lease_seconds=604800):
    return dict(original=original, resident=resident or w.resident, original_control=original_control,
                control=control or w.control, active_catalog=w.catalog, lease_seconds=lease_seconds)


@pytest.mark.asyncio
async def test_control_ceiling_does_not_grant_permissions_the_owner_cannot_delegate(tmp_path):
    from kdcube_ai_app.auth.tests.test_bound_session_issuance_store import counts

    async with _paired(tmp_path) as w:
        with pytest.raises(ResidentIssuancePending, match="agent_capability_grants_not_delegable"):
            await issue_descriptor_resident(w.service, {"user_id": OWNER}, **_request(w))
        assert await counts(w.sessions) == (0, 0, 0)
        assert await w.store.read_current_authority(subject_hash=w.subject_hash, access_id=w.resident.access_id) is None


async def _usable(w, bearer):
    from kdcube_ai_app.auth.bundle import BundleSessionAuthManager
    from kdcube_ai_app.auth.AuthManager import AuthenticationError
    from kdcube_ai_app.auth.tests.test_bound_session_issuer import authority

    try:
        return await BundleSessionAuthManager(authority=authority(w.sessions)).authenticate(bearer)
    except AuthenticationError:
        return None


@pytest.mark.asyncio
async def test_public_sync_first_create_begins_before_inactive_preparation_and_commits_both_cards(tmp_path, monkeypatch):
    from kdcube_ai_app.auth.tests.test_bound_session_issuer import resigned

    async with _paired(tmp_path) as w:
        prepare, plans = w.provider.prepare, []

        async def inspect(*, plan):
            row = await w.decisions.read(plan.transaction_id)
            assert row is not None and not row.terminal
            assert await w.store.read_current_authority(subject_hash=w.subject_hash, access_id=plan.access_id) is None
            prepared = await prepare(plan=plan)
            assert await _usable(w, await resigned(w.sessions, prepared.context)) is None
            plans.append(plan)
            return prepared

        monkeypatch.setattr(w.provider, "prepare", inspect)
        answer = await w.service.sync_agent_capability_control(USER, **w.arguments)
        assert answer["ok"], answer
        assert len(plans) == 1
        plan = plans[0]
        loaded = await w.service._load_record_any_state(plan.access_id, grantor_subject=OWNER)
        assert loaded and loaded[0].card_revision == 1 and loaded[0].expires_at == plan.expires_at
        assert await _usable(w, loaded[0].access_token) is not None
        row = await w.decisions.read(plan.transaction_id)
        assert row.state == "committed" and PARTICIPANT in row.finished
        stored = await w.authority.read_issuance_plan(plan.transaction_id)
        assert {member["access_id"] for member in stored["plan"]["intent"]["members"]} == {
            plan.access_id, w.control.access_id}
        assert plan.slots == ("access",) and plan.delivery_deadline == plan.expires_at
        assert w.service._minter.await_count == 0
        again = await w.service.sync_agent_capability_control(USER, **w.arguments)
        assert again["ok"] and (await w.service._load_record_any_state(plan.access_id, grantor_subject=OWNER))[0] == loaded[0]


@pytest.mark.asyncio
async def test_rotation_custody_cut_recovers_one_original_before_retiring_old_session(tmp_path, monkeypatch):
    async with _paired(tmp_path) as w:
        old = await issue_descriptor_resident(w.service, USER, **_request(w))
        control = (await w.service._load_record_any_state(w.control.access_id, grantor_subject=OWNER))[0]
        candidate = replace(old, card_revision=old.card_revision + 1)
        request = _request(w, original=old, resident=candidate, original_control=control, control=control)
        plan = await begin_descriptor_resident_issuance(w.service, USER, **request)
        original = await w.provider.prepare(plan=plan)

        async def fail(**kwargs):
            raise OSError("synthetic custody cut")

        monkeypatch.setattr(w.secrets, "create", fail)
        with pytest.raises(ResidentIssuancePending, match="resident_issuance_pending"):
            await issue_descriptor_resident(w.service, USER, **request)
        assert (await w.decisions.read(plan.transaction_id)).state == "committed"
        assert await _usable(w, old.access_token) is not None
        # SDK session validity alone is not Card admission. The real Card
        # publication fence stays closed while custody/ordered effects lag.
        with pytest.raises(CardStorageError, match="card_.*effects_pending"):
            await w.store.assert_current_version_readable(subject_hash=w.subject_hash, access_id=old.access_id)
        assert await begin_descriptor_resident_issuance(w.service, USER, **request) == plan
        w.recompose()  # fresh SDK provider, custody manager and handle store
        await w.service._card_coordinator[0].finish(plan.transaction_id)
        saved = await issue_descriptor_resident(w.service, USER, **request)
        assert saved.session_id == original.receipt.session_id and saved.access_token != old.access_token
        assert await _usable(w, saved.access_token) is not None
        assert await _usable(w, old.access_token) is None
        assert await w.provider.read(plan=plan) == original


@pytest.mark.asyncio
async def test_expired_resident_and_changed_control_rotate_under_one_committed_group(tmp_path, monkeypatch):
    async with _paired(tmp_path) as w:
        old = await issue_descriptor_resident(w.service, USER, **_request(w, lease_seconds=2))
        async with w.pool.acquire() as connection:
            await connection.execute("SELECT pg_sleep(2.1)")
        prepare, plans = w.provider.prepare, []

        async def inspect(*, plan):
            assert not (await w.decisions.read(plan.transaction_id)).terminal
            intent = await w.service._card_coordinator[1].load(plan.transaction_id)
            assert {member.candidate.access_id: member.action for member in intent.members} == {
                old.access_id: "renew", w.control.access_id: "update"}
            plans.append(plan)
            return await prepare(plan=plan)

        monkeypatch.setattr(w.provider, "prepare", inspect)
        arguments = _sync_arguments(allow_post=False)
        arguments["capability_authority"] = {**arguments["capability_authority"],
                                             "resource": w.arguments["capability_authority"]["resource"]}
        arguments.update(descriptor_revision="descriptor-narrower", descriptor_payload={"revision": "descriptor-narrower"})
        answer = await w.service.sync_agent_capability_control(USER, **arguments)
        assert answer["ok"] and len(plans) == 1, answer
        row = await w.decisions.read(plans[0].transaction_id)
        assert row.state == "committed" and PARTICIPANT in row.finished
        assert len(row.intent.payload["participant_inputs"][PARTICIPANT]["participant_candidates"][PARTICIPANT]["cards"]) == 2
        control = (await w.service._load_record_any_state(w.control.access_id, grantor_subject=OWNER))[0]
        resident = (await w.service._load_record_any_state(old.access_id, grantor_subject=OWNER))[0]
        assert (control.card_revision, resident.card_revision, resident.control_card.control_revision) == (2, 2, 2)
        assert resident.session_id != old.session_id and await _usable(w, resident.access_token) is not None
        assert await _usable(w, old.access_token) is None and w.service._minter.await_count == 0


@pytest.mark.asyncio
async def test_catalog_move_after_inactive_preparation_aborts_without_publishing_a_card(tmp_path, monkeypatch):
    from connection_hub.delegated_credentials.catalog.models import CatalogDocument
    from kdcube_ai_app.auth.tests.test_bound_session_issuance_store import counts

    async with _paired(tmp_path) as w:
        prepare, plans = w.provider.prepare, []

        async def move(*, plan):
            prepared = await prepare(plan=plan)
            plans.append(plan)
            changed = CatalogDocument.build({"delegated_credentials": {"oauth": {"enabled": True,
                "resources": [{"resource": "https://example.test/changed", "grants": ["changed:read"]}]}}})
            await w.catalog_store.write_version(changed)
            await w.catalog_store.publish_active(changed)
            return prepared

        monkeypatch.setattr(w.provider, "prepare", move)
        answer = await w.service.sync_agent_capability_control(USER, **w.arguments)
        assert answer["ok"] is False and len(plans) == 1
        row = await w.decisions.read(plans[0].transaction_id)
        assert row.state == "aborted"
        assert await w.store.read_current_authority(subject_hash=w.subject_hash, access_id=w.resident.access_id) is None
        assert await counts(w.sessions) == (1, 1, 0)  # prepared original, never a usable session


@pytest.mark.asyncio
async def test_pg_expiry_before_stage_records_abort_then_retires_inactive_original_and_retries(tmp_path):
    from kdcube_ai_app.auth.bundle.session_issuance import SessionIssuanceRefused

    async with _paired(tmp_path) as w:
        coordinator, intents, decisions, _ = w.service._card_coordinator
        w.service.bind_card_coordinator(coordinator, intents=intents, decisions=decisions, intent_ttl_seconds=1)
        request = _request(w)
        plan = await begin_descriptor_resident_issuance(w.service, USER, **request)
        await w.provider.prepare(plan=plan)  # crash before Hub reserve or STAGE
        assert not (await decisions.read(plan.transaction_id)).terminal
        async with w.pool.acquire() as connection:
            await connection.execute("SELECT pg_sleep(1.1)")
        row = await decisions.abort_expired(plan.transaction_id)
        assert row.state == "aborted"
        await coordinator.finish(plan.transaction_id)
        assert (await recover_resident_originals(w.service))["retired"] == 1
        assert (await recover_resident_originals(w.service))["retired"] == 0
        w.recompose()
        with pytest.raises(SessionIssuanceRefused, match="issuance_terminal"):
            await w.provider.prepare(plan=plan)
        second = await begin_descriptor_resident_issuance(w.service, USER, **request)
        assert second.transaction_id != plan.transaction_id and second.expires_at > plan.expires_at
        assert await begin_descriptor_resident_issuance(w.service, USER, **request) == second


@pytest.mark.asyncio
async def test_renew_policy_refusal_still_commits_descriptor_withdrawal(tmp_path):
    from connection_hub.delegated_credentials.agent_capability_control import AGENT_DESCRIPTOR_ISSUER_KIND
    from connection_hub.delegated_credentials.caller_writer_gate import CallerWriterRegistry
    from test_delegated_access_renewal import _Policy

    class RefuseRenew(_Policy):
        def _decision(self, request):
            self.allow = request.action != "renew"
            return super()._decision(request)

    async with _paired(tmp_path) as w:
        old = await issue_descriptor_resident(w.service, USER, **_request(w, lease_seconds=1))
        async with w.pool.acquire() as connection:
            await connection.execute("SELECT pg_sleep(1.1)")
        policy = RefuseRenew(True)
        registry = CallerWriterRegistry()
        registry.register(AGENT_DESCRIPTOR_ISSUER_KIND, policy)
        w.service.bind_caller_writers(registry)
        arguments = _sync_arguments(allow_post=False)
        arguments["capability_authority"] = {**arguments["capability_authority"],
                                             "resource": w.arguments["capability_authority"]["resource"]}
        arguments.update(descriptor_revision="descriptor-withdrawal", descriptor_payload={"revision": "descriptor-withdrawal"})
        answer = await w.service.sync_agent_capability_control(USER, **arguments)
        assert answer["ok"] is False and answer["reason"] == "pb_refused"
        assert answer["control_committed"] and answer["card_committed"]
        control = (await w.service._load_record_any_state(w.control.access_id, grantor_subject=OWNER))[0]
        assert control.card_revision == w.control.card_revision + 1
        assert ("decide", "renew") in policy.calls and ("decide", "update") in policy.calls
        narrowed = (await w.service._load_record_any_state(old.access_id, grantor_subject=OWNER))[0]
        assert narrowed.control_card.control_revision == control.card_revision
        assert narrowed.expires_at == old.expires_at
        assert await _usable(w, old.access_token) is None
        # The expired handle was not re-wrapped, revived, or removed to make
        # the fallback succeed. A later permitted renewal still recovers it.
        policy.allow = True
        registry = CallerWriterRegistry()
        registry.register(AGENT_DESCRIPTOR_ISSUER_KIND, _Policy(True))
        w.service.bind_caller_writers(registry)
        recovered = await w.service.sync_agent_capability_control(USER, **arguments)
        assert recovered["ok"], recovered
        live = (await w.service._load_record_any_state(old.access_id, grantor_subject=OWNER))[0]
        assert live.access_id == old.access_id and live.expires_at > old.expires_at
        assert await _usable(w, live.access_token) is not None


@pytest.mark.asyncio
async def test_consent_requires_descriptor_sync_before_first_creation_and_rotates_an_expired_resident(tmp_path):
    from test_descriptor_agent_consent import _consent

    async with _paired(tmp_path) as w:
        missing = await _consent(w.service, w.resident)
        assert missing["error"] == "agent_descriptor_resident_sync_required"
        assert w.service._minter.await_count == 0
        old = await issue_descriptor_resident(w.service, USER, **_request(w, lease_seconds=1))
        async with w.pool.acquire() as connection:
            await connection.execute("SELECT pg_sleep(1.1)")
        answer = await _consent(w.service, old)
        assert answer["ok"] and answer["access_token"]
        saved = (await w.service._load_record_any_state(old.access_id, grantor_subject=OWNER))[0]
        assert saved.card_revision == old.card_revision + 1 and saved.session_id != old.session_id
        assert answer["access_token"] == saved.access_token and await _usable(w, saved.access_token) is not None
        assert w.service._minter.await_count == 0


@pytest.mark.asyncio
async def test_selection_rotates_an_expired_resident_without_using_the_active_minter(tmp_path):
    from test_descriptor_agent_consent import READ, _policy_with_named_operations

    async with _paired(tmp_path) as w:
        old = await issue_descriptor_resident(w.service, USER, **_request(w, lease_seconds=1))
        async with w.pool.acquire() as connection:
            await connection.execute("SELECT pg_sleep(1.1)")
        choice = {**_policy_with_named_operations(READ), "resource": w.arguments["capability_authority"]["resource"]}
        answer = await w.service.update_agent_capability_selection(USER, access_id=old.access_id,
            expected_card_revision=old.card_revision, selected_capabilities=choice)
        assert answer["ok"], answer
        saved = (await w.service._load_record_any_state(old.access_id, grantor_subject=OWNER))[0]
        assert saved.card_revision == old.card_revision + 1 and saved.session_id != old.session_id
        assert await _usable(w, saved.access_token) is not None and w.service._minter.await_count == 0



@pytest.mark.asyncio
async def test_pg_card_expiry_after_committed_custody_cut_finishes_without_installing_or_replacing_original(tmp_path, monkeypatch):
    from kdcube_ai_app.auth.tests.test_bound_session_issuance_store import counts

    async with _paired(tmp_path) as w:
        plan = await begin_descriptor_resident_issuance(w.service, USER, **_request(w, lease_seconds=2))
        prepared = await w.provider.prepare(plan=plan)
        await w.service.reserve_oauth_issuance(plan=plan, slot="access", token_sha256=prepared.receipt.bearer_sha256,
            record=prepared.record, ttl_seconds=prepared.ttl_seconds)

        async def fail(**kwargs):
            raise OSError("synthetic custody cut")

        monkeypatch.setattr(w.secrets, "create", fail)
        result = await w.service.complete_oauth_issuance(transaction_id=plan.transaction_id)
        assert result.state == "pending" and (await w.decisions.read(plan.transaction_id)).state == "committed"
        before = await counts(w.sessions)
        async with w.pool.acquire() as connection:
            await connection.execute("SELECT pg_sleep(2.1)")
        w.recompose()
        result = await w.service.complete_oauth_issuance(transaction_id=plan.transaction_id)
        assert result.state == "committed" and result.per_slot["access"].outcome == "superseded"
        assert await counts(w.sessions) == before  # no new reservation, signing or session activation
        assert (await w.metadata.read_current(plan.access_id)) is None
        # A second fresh composition must reconstruct the same public outcome
        # even though the SQL reservation was activated before the custody cut.
        assert (await w.authority.issuance_reservations(plan.transaction_id))["access"]["state"] == "activated"
        w.recompose()
        replay = await w.service.complete_oauth_issuance(transaction_id=plan.transaction_id)
        assert replay.state == "committed" and replay.per_slot["access"].outcome == "superseded"
        assert await counts(w.sessions) == before
        assert (await w.metadata.read_current(plan.access_id)) is None
