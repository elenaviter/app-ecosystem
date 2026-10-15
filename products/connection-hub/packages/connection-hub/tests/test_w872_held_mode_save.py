"""W872: a Card save carries the mode the person switched on an operation the Card already holds.

Operator, 2026-10-15 ~18:47Z: "when i click through the card during edit THIS MUST NOT CAUSE NO ANY CALLS! i must
simply be able normally pick what i need! only when i press save someting can be asked". The editor makes no call
on a switch; the Save sends the switched modes, and the server commits them as that save's own policy effects.
Real filesystem Cards, policies and coordinator (test_w756_local_whole_save); stand-in catalog/resolution ports.
"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.whole_card_save import (
    WholeCardSaveRefused, added_invocation_modes, held_mode_changes,
)
from test_w756_local_whole_save import CHOICES, RESOURCE, SUBJECT_HASH, _authority, _world
from test_w756_whole_card_diff import Catalog, original


def test_held_modes_accepts_a_held_operation_and_still_refuses_unknown_and_removed():
    held = original()
    # Default (W749): a held key refuses, so no other caller starts writing held policies.
    with pytest.raises(WholeCardSaveRefused, match="invocation_modes_invalid"):
        added_invocation_modes(original=held, candidate=held, config=Catalog(),
                               modes={"service": {"read": "once"}}, policy_bearing=True)
    assert added_invocation_modes(original=held, candidate=held, config=Catalog(),
                                  modes={"service": {"read": "once"}}, policy_bearing=True,
                                  held_modes=True) == {"service": {"read": "once"}}
    assert held_mode_changes(held, {"service": {"read": "once"}}) is True
    assert held_mode_changes(held, {"service": {"write": "once"}}) is False  # an addition, not a held change
    for modes in ({"service": {"write": "once"}}, {"other": {"read": "once"}}):  # not on the Card / unknown
        with pytest.raises(WholeCardSaveRefused, match="invocation_modes_invalid"):
            added_invocation_modes(original=held, candidate=held, config=Catalog(), modes=modes,
                                   policy_bearing=True, held_modes=True)
    removed = original(resource_operations={"service": ()})
    with pytest.raises(WholeCardSaveRefused, match="invocation_modes_invalid"):
        added_invocation_modes(original=held, candidate=removed, config=Catalog(),
                               modes={"service": {"read": "once"}}, policy_bearing=True, held_modes=True)
    # An added operation still needs its mode beside a held one.
    grown = original(resource_operations={"service": ("read", "write")})
    with pytest.raises(WholeCardSaveRefused, match="invocation_mode_required"):
        added_invocation_modes(original=held, candidate=grown, config=Catalog(),
                               modes={"service": {"read": "once"}}, policy_bearing=True, held_modes=True)


async def _screen_world(tmp_path):
    host, store, decisions, before, after, policies, effects = await _world(tmp_path)
    from connection_hub.delegated_credentials.catalog.models import CatalogDocument

    class LocalCatalog(Catalog):
        resource_config = Catalog.card_selector_config

    host._issuer_actor_subject_bound = False
    host._issuer_actor_subject = ""
    host._tenant, host._project = "synthetic", "synthetic"
    host._persistence.load_current = host._persistence.load

    async def active():
        return replace(CatalogDocument.build({}), version=before.catalog_version)

    async def config(*args, **kwargs):
        return LocalCatalog()

    async def resolve(**kwargs):
        return SimpleNamespace(error=None, revoke=False,
            resource_grants=before.resource_grants, resource_operations=kwargs["resource_operations"],
            named_service_operations=before.named_service_operations, named_services=before.named_services,
            operations=before.operations, account_scope=before.account_scope,
            properties=before.properties, identity_scope=before.identity_scope,
            reconciled=SimpleNamespace(to_public_dict=lambda: {}))

    async def notify(*args, **kwargs):
        return None

    host._active_catalog, host._catalog_config = active, config
    host._resolve_card_authority, host.notify_change = resolve, notify
    # The decision-store stub binds every intent to one fixed transaction id; production gives each save its own,
    # so consecutive saves of one Card are possible here as they are live.
    begin, counter = decisions.begin, iter(range(1, 1000))

    async def fresh_begin(draft, *, transaction_id=None, epoch=None, connection=None):
        return await begin(draft, transaction_id=transaction_id or f"{next(counter):064x}", epoch=epoch,
                           connection=connection)

    decisions.begin = fresh_begin
    return host, store, decisions, before, after, policies


async def _save(host, before, revision, modes, decisions=None):
    answer = await host.update_access({"user_id": before.grantor_subject}, access_id=before.access_id,
        resource_grants=before.resource_grants, resource_operations=_save.selection,
        expected_card_revision=revision, expected_catalog_version=before.catalog_version,
        label="W872 save", _screen_save=True, invocation_modes=modes)
    if decisions is not None and answer.get("ok") and not answer.get("unchanged"):
        # FINISH the committed decision, as the coordinator does after a commit, before the next save.
        row = list(decisions.rows.values())[-1]
        await host._card_coordinator[0].finish(row.transaction_id)
    return answer


async def _held_world(tmp_path, start_mode):
    """A Card that already holds the CHOICES operations, each policy set to ``start_mode`` without a Card-save
    marker (a later save's PREPARE meeting an earlier save's COMMITTED marker is W873, settled separately)."""
    from connection_hub.delegated_credentials.automation_access import record_from_card

    host, store, decisions, before, after, policies = await _screen_world(tmp_path)
    await host._persist_record(record_from_card(after), expected_revision=before.card_revision, effects=[],
                               require_transaction=True)
    row = list(decisions.rows.values())[-1]
    await host._card_coordinator[0].finish(row.transaction_id)
    for operation in CHOICES:
        await policies.set_policy(owner_subject=before.grantor_subject, authority=_authority(after, operation),
                                  mode=start_mode)
    revision = (await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=before.access_id))[1].card_revision
    _save.selection = after.resource_operations
    return host, store, decisions, before, after, policies, revision


@pytest.mark.asyncio
@pytest.mark.parametrize("start, switched", [("once", "always"), ("always", "once")])
async def test_a_mode_only_save_commits_the_policy_in_either_direction(tmp_path, start, switched):
    host, store, decisions, before, after, policies, revision = await _held_world(tmp_path, start)
    answer = await _save(host, before, revision, {RESOURCE: {"one": switched}}, decisions)
    # Only the mode of a held operation changes: not "Nothing to save", and the policy is written by the save.
    assert answer["ok"] is True and not answer.get("unchanged"), answer
    assert decisions.decisions[-1] == "committed" and host._persistence.direct == []
    policy = await policies.get(owner_subject=before.grantor_subject, authority=_authority(after, "one"))
    assert (policy.mode, policy.revision) == (switched, 2)
    for operation in ("two", "three"):  # untouched by the save
        other = await policies.get(owner_subject=before.grantor_subject, authority=_authority(after, operation))
        assert (other.mode, other.revision) == (start, 1)
    card = (await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=before.access_id))[1]
    assert card.resource_operations == after.resource_operations


@pytest.mark.asyncio
async def test_a_save_with_no_change_and_no_mode_is_still_nothing_to_save(tmp_path):
    host, store, decisions, before, after, policies = await _screen_world(tmp_path)
    _save.selection = after.resource_operations
    first = await _save(host, before, before.card_revision, {RESOURCE: CHOICES}, decisions)
    revision = (await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=before.access_id))[1].card_revision
    assert first["ok"] is True
    again = await _save(host, before, revision, {})
    assert again == {"ok": True, "unchanged": True}
    assert decisions.decisions == ["committed"]
