"""W578: with Card transactions on, a Control-bound Card's authority changes only through its owner's transaction.

A person's My Card or a project-bound agent Card carries authority its
Control's owner relies on (under an AND composition a project needs its usable
administrators' My Cards), so creating one, replacing its credentials or any
change to its authority fields bypasses that owner's decision and refuses
until it is enlisted. A prolongation (expiry forward, credentials unchanged)
and a governed edit of display fields still pass. With transactions off
nothing changes.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from connection_hub.delegated_credentials.automation_access import record_from_card
from connection_hub.delegated_credentials.caller_writer_gate import CallerWriteRefused
from connection_hub.delegated_credentials.cards.model import CardCredentialHandles, ControlCardBinding
from connection_hub.delegated_credentials.caller_writer_gate import CallerWrite
from test_card_coordinated_writes import _host
from test_delegated_access_renewal import _Policy, _registry
from test_card_service import SUBJECT_HASH
from test_card_transaction_store import _visible

BINDING = ControlCardBinding(control_id="c-1", issuer_ref="work:project:one", issuer_kind="project",
                             control_revision=1)


async def _bound(tmp_path):
    host, store, decisions, before, _ = await _host(tmp_path)
    bound = replace(before, card_revision=before.card_revision + 1, control_card=BINDING)
    service = next(iter(host._card_coordinator[0].participants.values()))._service
    await service.commit(bound, subject_hash=SUBJECT_HASH, expected_revision=before.card_revision,
                         now=1_780_000_000)
    host._caller_writers = _registry(_Policy(True))  # the binding's caller writer allows; this rule still holds
    return host, store, bound


def _persist(host, card, *, expected, action="update"):
    return host._persist_record(record_from_card(card), expected_revision=expected,
                                caller_write=CallerWrite(action, "person-1"))


def _next(card, **changes):
    return replace(card, card_revision=card.card_revision + 1, **changes)


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"identity_scope": "workspace"},
    {"resource_grants": {}},
    {"account_scope": {"google": {"acct-1": ("mail.read",)}}},
    {"properties": {"x": 1}},
    {"control_card": None},
    {"composition_mode": "or"},
])
async def test_an_authority_change_to_a_bound_card_refuses(tmp_path, changes):
    host, store, bound = await _bound(tmp_path)
    with pytest.raises(CallerWriteRefused, match="card_transactions_direct_write_refused"):
        await _persist(host, _next(bound, **changes), expected=bound.card_revision)
    assert await _visible(store, bound) == bound


@pytest.mark.asyncio
@pytest.mark.parametrize("action,changes", [("prolong", "expiry"), ("update", "label")])
async def test_a_prolongation_or_a_display_edit_of_a_bound_card_passes(tmp_path, action, changes):
    host, store, bound = await _bound(tmp_path)
    edited = (_next(bound, expires_at=bound.expires_at + 3600) if changes == "expiry"
              else _next(bound, label="renamed"))
    await _persist(host, edited, expected=bound.card_revision, action=action)
    assert await _visible(store, bound) == edited


@pytest.mark.asyncio
async def test_a_prolongation_hiding_an_authority_change_or_moving_expiry_back_refuses(tmp_path):
    host, store, bound = await _bound(tmp_path)
    for candidate in (_next(bound, expires_at=bound.expires_at + 3600, composition_mode="or"),
                      _next(bound, expires_at=bound.expires_at - 60)):
        with pytest.raises(CallerWriteRefused, match="card_transactions_direct_write_refused"):
            await _persist(host, candidate, expected=bound.card_revision, action="prolong")
    assert await _visible(store, bound) == bound


@pytest.mark.asyncio
async def test_creating_a_bound_card_or_replacing_its_credentials_refuses(tmp_path):
    host, store, bound = await _bound(tmp_path)
    with pytest.raises(CallerWriteRefused, match="card_transactions_direct_write_refused"):
        await host._persist_record(record_from_card(replace(bound, access_id="new-card", card_revision=1)),
                                   expected_revision=0)

    real_load = host._persistence.load

    async def other_handles(access_id, *, subject_hash):
        current, _ = await real_load(access_id, subject_hash=subject_hash)
        return current, CardCredentialHandles(access_id=access_id, session_id="another-session")

    host._persistence.load = other_handles
    with pytest.raises(CallerWriteRefused, match="card_transactions_direct_write_refused"):
        await _persist(host, _next(bound, label="same authority"), expected=bound.card_revision)


@pytest.mark.asyncio
async def test_with_transactions_off_a_bound_card_edit_is_unchanged(tmp_path):
    host, store, bound = await _bound(tmp_path)
    del host._card_coordinator
    edited = _next(bound, composition_mode="or")
    await _persist(host, edited, expected=bound.card_revision)
    assert host._persistence.direct == [edited.card_revision]  # the direct path, as before
