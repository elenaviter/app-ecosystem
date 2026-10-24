"""W941: a Save refused delegated_access_grants_not_delegable tells the person which permissions were refused.

The planner keeps the refused permission names, the lifecycle plan hands them to STAGE, and STAGE signs
them on that one named refusal: plain grant names only, sorted and bounded. Every other refusal keeps
the fixed {kind, code, status} shape.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.cards import participant_card_version as version
from connection_hub.delegated_credentials.managed_card_selection_plan import GrantsNotDelegable
from test_w661_card_version_operation import _operation, _Planner, _request, _verified
from test_w941_agent_card_governed import _Host, _plan

NOT_DELEGABLE = "delegated_access_grants_not_delegable"


@pytest.mark.asyncio
async def test_stage_signs_the_refused_permission_names_on_the_named_refusal():
    planner = _Planner({"ok": False, "error": NOT_DELEGABLE, "status": 409,
                        "grants": ["named_services:use", "docs:read", "docs:read"]})
    operation, _, store = _operation(planner=planner)
    request = _request()
    assert _verified(await operation.answer(request), request) == {
        "kind": "refused", "code": NOT_DELEGABLE, "status": 409, "grants": ["docs:read", "named_services:use"]}
    assert store.staged == []


@pytest.mark.asyncio
async def test_without_names_the_refusal_keeps_its_fixed_shape():
    operation, _, _ = _operation(planner=_Planner({"ok": False, "error": NOT_DELEGABLE, "status": 409}))
    request = _request()
    assert _verified(await operation.answer(request), request) == {
        "kind": "refused", "code": NOT_DELEGABLE, "status": 409}


@pytest.mark.asyncio
async def test_no_other_refusal_ever_carries_names():
    operation, _, _ = _operation(planner=_Planner({"ok": False, "error": "card_grantor_proof_invalid",
                                                  "status": 409, "grants": ["docs:read"]}))
    request = _request()
    assert _verified(await operation.answer(request), request) == {
        "kind": "refused", "code": "card_grantor_proof_invalid", "status": 409}
    assert version.CardVersionRefused("card_changed", grants=["docs:read"]).grants == ()


def test_only_plain_names_are_sent_sorted_and_bounded():
    assert version.refused_grant_names(["b:read", "a:read", "", " x", "x y", "a\nb", 7, None, "-x", "z" * 129]) == (
        "a:read", "b:read")
    assert version.refused_grant_names("docs:read") == () and version.refused_grant_names(None) == ()
    many = [f"g{index:03d}:read" for index in range(100)]
    assert version.refused_grant_names(many) == tuple(sorted(many)[:version.MAX_REFUSED_GRANTS])


class _RefusingHost(_Host):
    async def _resolve_card_authority(self, **kwargs):
        return SimpleNamespace(error={"error": NOT_DELEGABLE, "grants": ["named_services:use", "docs:read"]},
                               revoke=False)


def test_the_planner_keeps_the_refused_names():
    with pytest.raises(GrantsNotDelegable) as refused:
        _plan(_RefusingHost(), governed=None)
    assert refused.value.reason == NOT_DELEGABLE and str(refused.value) == NOT_DELEGABLE
    assert refused.value.grants == ("docs:read", "named_services:use")


def test_the_lifecycle_plan_hands_the_names_to_stage():
    from connection_hub.delegated_credentials import card_lifecycle_plan

    source = inspect.getsource(card_lifecycle_plan)
    assert 'if getattr(exc, "grants", None):' in source and 'refused["grants"] = list(exc.grants)' in source
