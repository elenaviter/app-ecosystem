"""W661 S5 (EMain 18:22Z): a manual reselect keeps PB's protected grants exactly as the original holds them.

PB reads no Cards, so the comparison is the Hub's: each listed grant of each listed resource must be
held by the candidate exactly as by the original. Added or removed refuses; the Hub compares only the
grants it is given. Real planner; W607's catalog-resolution stand-in.
"""

from __future__ import annotations

import dataclasses

import pytest

from test_w607_reselect_plan_integration import _Host, _cards, _plan, _update

PROTECTED = {"service-a": ["work:admin"]}


def _with(card, grants):
    return dataclasses.replace(card, resource_grants={"service-a": tuple(grants)})


@pytest.mark.asyncio
async def test_a_manual_edit_cannot_add_a_protected_grant():
    project, control, my = _cards()
    control = _with(control, ["work:read"])
    update = {**_update(control, {"resource_grants": {"service-a": ["work:read", "work:admin"]}}),
              "protected_grants": PROTECTED}
    result = await _plan(_Host(project, control, my), [update])
    assert result == {"ok": False, "error": "card_edit_admin_grant_role_only", "status": 409}


@pytest.mark.asyncio
async def test_a_manual_edit_cannot_remove_a_protected_grant():
    project, control, my = _cards()
    control = _with(control, ["work:read", "work:admin"])
    update = {**_update(control, {"resource_grants": {"service-a": ["work:read"]}}), "protected_grants": PROTECTED}
    result = await _plan(_Host(project, control, my), [update])
    assert result == {"ok": False, "error": "card_edit_admin_grant_role_only", "status": 409}


@pytest.mark.asyncio
@pytest.mark.parametrize("card_name", ["control", "my"])
async def test_an_ordinary_edit_while_holding_the_protected_grant_is_saved(card_name):
    project, control, my = _cards()
    control, my = _with(control, ["work:read", "work:admin"]), _with(my, ["work:read", "work:admin"])
    card = control if card_name == "control" else my
    update = {**_update(card, {"resource_grants": {"service-a": ["work:admin", "work:write"]}}),
              "protected_grants": PROTECTED}
    result = await _plan(_Host(project, control, my), [update])
    assert result["ok"] is True, result
    [member] = result["plan"]["candidate_value"]["cards"]
    assert set(member["candidate"]["resource_grants"]["service-a"]) == {"work:admin", "work:write"}


@pytest.mark.asyncio
@pytest.mark.parametrize("protected", [
    {}, {"service-a": []}, {"service-a": ["work:admin", "work:admin"]}, {"service-a": [" work:admin"]},
    {f"r{i}": ["g"] for i in range(5)}, {"service-a": [f"g{i}" for i in range(17)]}, ["work:admin"],
    {"service-a": [{}]}, {"service-a": [[]]},  # unhashable entries refuse by name, never TypeError
])
async def test_a_malformed_protected_list_is_refused(protected):
    project, control, my = _cards()
    update = {**_update(control, {"resource_grants": {"service-a": ["work:read"]}}), "protected_grants": protected}
    result = await _plan(_Host(project, control, my), [update])
    assert result["ok"] is False and result["error"] == "card_plan_update_invalid"


@pytest.mark.asyncio
async def test_only_a_manual_reselect_carries_protected_grants():
    project, control, my = _cards()
    update = {**_update(control, {"resource_grants": {}}), "kind": "reselect_project_control",
              "protected_grants": PROTECTED}
    result = await _plan(_Host(project, control, my), [update])
    assert result["ok"] is False and result["error"] == "card_plan_update_invalid"
