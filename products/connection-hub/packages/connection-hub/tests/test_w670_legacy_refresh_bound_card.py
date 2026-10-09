"""W670 line 2: a refresh of a Control-bound Card keeps the agent connected once Card transactions are on.

Live (dev-main, 2026-10-09 ~16:38Z): Hub log "refresh issuance failed after rotation
reason=temporarily_unavailable status=503 restored=True", then "token withheld: caller write refused
client=dcr-...". A refresh rotation re-records the agent's (or person's) existing Card through
``record_oauth_grant`` (action ``oauth_grant``) with the same authority, a later expiry and the newly
rotated OAuth tokens. ``_refuse_bound_direct_write`` refused it as a credential change.

Card transactions require the PostgreSQL authority, whose handle store keeps no token for an OAuth Card:
the access and refresh tokens live in the OAuth authority store, which the token route rotates only after
checking the presented refresh token and the live Card. Compared in the store's own terms the Card's
credential handles do not change, so the rotation is a prolongation and commits as one Card transaction.
A handle the store does hold (a session, or a resident bearer) still refuses when it changes.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from connection_hub.delegated_credentials.automation_access import record_from_card
from connection_hub.delegated_credentials.caller_writer_gate import CallerWrite, CallerWriteRefused
from connection_hub.delegated_credentials.cards.model import CardCredentialHandles
from test_card_transaction_store import _visible
from test_w578_bound_card_writes import _bound


def _refresh_rotation(card, *, session_id=""):
    """What a refresh rotation persists: the same Card and authority, a later expiry, the rotated tokens."""
    rotated = replace(card, card_revision=card.card_revision + 1, expires_at=card.expires_at + 3600)
    return rotated, record_from_card(rotated, CardCredentialHandles(
        access_id=card.access_id, access_token="rotated-access-token", refresh_token="rotated-refresh-token",
        session_id=session_id))


def _rotate(host, record, bound):
    return host._persist_record(record, expected_revision=bound.card_revision,
                                caller_write=CallerWrite("oauth_grant", "person-1"))


@pytest.mark.asyncio
async def test_a_refresh_rotation_of_a_bound_card_commits_as_one_card_transaction(tmp_path):
    host, store, bound = await _bound(tmp_path)
    rotated, record = _refresh_rotation(bound)
    await _rotate(host, record, bound)
    visible = await _visible(store, bound)
    # The authority and the Control binding are carried forward; only the revision and expiry move.
    assert visible == rotated
    assert visible.control_card == bound.control_card
    decisions = host._card_coordinator[2]
    assert decisions.decisions == ["committed"] and host._persistence.direct == []


@pytest.mark.asyncio
async def test_two_refreshes_in_a_row_both_commit(tmp_path):
    host, store, bound = await _bound(tmp_path)
    decisions = host._card_coordinator[2]
    real_begin, issued = decisions.begin, iter(range(1, 100))

    async def fresh_transaction(draft, **kwargs):
        # The test decision store hands out one fixed id; a real log gives each transaction its own.
        kwargs.setdefault("transaction_id", f"{next(issued):064x}")
        return await real_begin(draft, **kwargs)

    decisions.begin = fresh_transaction
    first, record = _refresh_rotation(bound)
    await _rotate(host, record, bound)
    second, record = _refresh_rotation(first)
    await _rotate(host, record, first)
    assert await _visible(store, bound) == second
    assert host._card_coordinator[2].decisions == ["committed", "committed"]


@pytest.mark.asyncio
async def test_a_refresh_that_changes_a_handle_the_store_holds_still_refuses(tmp_path):
    host, store, bound = await _bound(tmp_path)
    _, record = _refresh_rotation(bound, session_id="another-session")
    with pytest.raises(CallerWriteRefused, match="card_transactions_direct_write_refused"):
        await _rotate(host, record, bound)
    assert await _visible(store, bound) == bound


@pytest.mark.asyncio
async def test_a_bearer_held_by_the_store_still_refuses_when_it_changes(tmp_path):
    # A resident agent Card's bearer (or a pre-cutover Redis record) IS a stored handle.
    host, store, bound = await _bound(tmp_path)
    real_load = host._persistence.load

    async def bearer_held(access_id, *, subject_hash):
        current, _ = await real_load(access_id, subject_hash=subject_hash)
        return current, CardCredentialHandles(access_id=access_id, access_token="resident-bearer")

    host._persistence.load = bearer_held
    _, record = _refresh_rotation(bound)
    with pytest.raises(CallerWriteRefused, match="card_transactions_direct_write_refused"):
        await _rotate(host, record, bound)
    assert await _visible(store, bound) == bound


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"identity_scope": "workspace"}, {"resource_grants": {}},
                                     {"control_card": None}, {"expires_at": 1}])
async def test_a_refresh_cannot_carry_an_authority_change_or_an_earlier_expiry(tmp_path, changes):
    host, store, bound = await _bound(tmp_path)
    rotated, _ = _refresh_rotation(bound)
    changed = replace(rotated, **changes)
    record = record_from_card(changed, CardCredentialHandles(
        access_id=bound.access_id, access_token="rotated-access-token", refresh_token="rotated-refresh-token"))
    with pytest.raises(CallerWriteRefused):
        await _rotate(host, record, bound)
    assert await _visible(store, bound) == bound
