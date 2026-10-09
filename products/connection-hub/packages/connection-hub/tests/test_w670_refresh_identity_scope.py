"""W670 line 2: a refresh rotation keeps the Card's identity scope when its refresh record names none.

A refresh re-records the Card through ``record_oauth_grant`` with the identity scope stored on the refresh
record. A record issued through the original exchange keeps no identity scope, so without carrying the
Card's own value a rotation would clear it: a change to an authority field, refused under Card transactions
for a Control-bound Card and a silent narrowing otherwise. A reviewed consent still sets it.
"""

from __future__ import annotations

import pytest

from test_oauth_card_extension import CLIENT, CONCRETE
from test_resident_profile_cards import GRANTOR, _Harness


async def _grant(harness, *, identity_scope, replace_authority, access_token, refresh_token):
    return await harness.service.record_oauth_grant(
        grantor_subject=GRANTOR, client_id=CLIENT, client_label="Claude Code", scopes=["memories:read"],
        resource=CONCRETE, access_token=access_token, refresh_token=refresh_token,
        identity_scope=identity_scope, replace_authority=replace_authority,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("presented", [None, ""])
async def test_a_refresh_without_an_identity_scope_keeps_the_cards(tmp_path, presented):
    harness = _Harness(tmp_path)
    issued = await _grant(harness, identity_scope="workspace", replace_authority=True,
                          access_token="at-1", refresh_token="rt-1")
    assert issued is not None and issued.identity_scope == "workspace"
    rotated = await _grant(harness, identity_scope=presented, replace_authority=False,
                           access_token="at-2", refresh_token="rt-2")
    assert rotated is not None and rotated.identity_scope == "workspace"
    assert rotated.card_revision == issued.card_revision + 1


@pytest.mark.asyncio
async def test_a_reviewed_consent_still_sets_the_identity_scope(tmp_path):
    harness = _Harness(tmp_path)
    await _grant(harness, identity_scope="workspace", replace_authority=True, access_token="at-1",
                 refresh_token="rt-1")
    reviewed = await _grant(harness, identity_scope="", replace_authority=True, access_token="at-2",
                            refresh_token="rt-2")
    assert reviewed is not None and reviewed.identity_scope == ""
