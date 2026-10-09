"""W670 line 2 reproduction (red): a refresh of a Control-bound Card is refused once Card transactions are on.

Live (dev-main, 2026-10-09 ~16:38Z): Hub log "refresh issuance failed after rotation
reason=temporarily_unavailable status=503 restored=True", then "token withheld: caller write refused
client=dcr-...". The SDK token route (oauth/http/routes.py ~3418-3437) answers a refresh with 503 when
``record_oauth_grant`` raises ``CallerWriteRefused``.

Mechanism, at AE 12392311: a refresh rotation re-records the agent's (or person's) existing Card with the
same authority, a later expiry and NEW access/refresh tokens. With Card transactions on,
``_refuse_bound_direct_write`` (automation_access.py ~2726-2733) refuses any write to a Control-bound Card
whose credential handles change, so every refresh of a grant issued before activation is withheld and the
agent is cut off at its next refresh.

The first test pins today's refusal exactly. The second states the required behaviour (operator, via
Main's W670 release scope line 2: "no cut-off agents, no re-consent") and is a strict xfail: it turns
into a failure, the signal to drop the marker, the moment the fix lets the refresh through. This file is
the reproduction only; the fix is claude-app@e-home's.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from connection_hub.delegated_credentials.automation_access import record_from_card
from connection_hub.delegated_credentials.caller_writer_gate import CallerWrite, CallerWriteRefused
from connection_hub.delegated_credentials.cards.model import CardCredentialHandles
from test_card_transaction_store import _visible
from test_w578_bound_card_writes import _bound


def _refresh_rotation(card):
    """What a refresh rotation persists: the same Card and authority, a later expiry, new tokens."""
    rotated = replace(card, card_revision=card.card_revision + 1, expires_at=card.expires_at + 3600)
    return record_from_card(rotated, CardCredentialHandles(
        access_id=card.access_id, access_token="rotated-access-token", refresh_token="rotated-refresh-token"))


@pytest.mark.asyncio
async def test_today_a_refresh_of_a_bound_card_is_refused_with_card_transactions_on(tmp_path):
    host, store, bound = await _bound(tmp_path)
    with pytest.raises(CallerWriteRefused, match="card_transactions_direct_write_refused"):
        await host._persist_record(_refresh_rotation(bound), expected_revision=bound.card_revision,
                                   caller_write=CallerWrite("update", "person-1"))
    assert await _visible(store, bound) == bound  # nothing written; the route then answers 503


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, raises=CallerWriteRefused,
                   reason="W670 line 2: a refresh of a pre-activation grant on a bound Card is refused today")
async def test_a_refresh_rotation_of_a_bound_card_keeps_the_agent_connected(tmp_path):
    host, store, bound = await _bound(tmp_path)
    record = _refresh_rotation(bound)
    await host._persist_record(record, expected_revision=bound.card_revision,
                               caller_write=CallerWrite("update", "person-1"))
    visible = await _visible(store, bound)
    # The authority is carried forward unchanged; only the expiry and the credentials move.
    assert visible.card_revision == bound.card_revision + 1
    assert visible.control_card == bound.control_card
    assert replace(visible, card_revision=bound.card_revision, expires_at=bound.expires_at) == bound

