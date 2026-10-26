"""W997: a Save refused invalid_named_service_operation_selection names each action and the grants it lacks.

Live on dev-main, 26 Oct: ticking every Documents action on a project Control Card selected the two
Drive-file actions (list_folder needs drive:read, upload_file drive:write) without those grants; the
planner refused and the person read only "The Card save was refused by the Hub". Now the narrowing keeps
each action with its missing grants, the planner and lifecycle plan hand them to STAGE, and STAGE signs
them on that one named refusal: plain names only, sorted and bounded.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.cards import participant_card_version as version
from connection_hub.delegated_credentials.managed_card_selection_plan import NamedServiceOperationsRefused
from connection_hub.delegated_credentials.named_service_policy import (
    NamedServiceOperationsLackGrants,
    narrow_named_service_config,
)
from test_w661_card_version_operation import _operation, _Planner, _request, _verified
from test_w941_agent_card_governed import _Host, _plan

SELECTION = "invalid_named_service_operation_selection"
DOCS = {
    "namespaces": {
        "docs": {
            "tools": {
                "action": {
                    "grants": ["named_services:use"],
                    "operations": {
                        "object.action.export": {"grants": ["named_services:use", "docs:read"]},
                        "object.action.list_folder": {"grants": ["named_services:use", "drive:read"]},
                        "object.action.upload_file": {"grants": ["named_services:use", "drive:write"]},
                    },
                },
            },
        },
    },
}
ROWS = [
    {"namespace": "docs", "operation": "object.action.list_folder", "grants": ["drive:read"]},
    {"namespace": "docs", "operation": "object.action.upload_file", "grants": ["drive:write"]},
]


def test_the_narrowing_names_each_action_with_the_grants_it_lacks():
    with pytest.raises(NamedServiceOperationsLackGrants) as refused:
        narrow_named_service_config(
            config=DOCS, resource="named_services",
            selected={"docs": ["object.action.export", "object.action.list_folder", "object.action.upload_file"]},
            grants=["named_services:use", "docs:read", "docs:write", "docs:comment"],
        )
    assert refused.value.operations == (
        ("docs", "object.action.list_folder", ("drive:read",)),
        ("docs", "object.action.upload_file", ("drive:write",)),
    )
    # Still a ValueError with the same text, so every existing handler keeps working.
    assert isinstance(refused.value, ValueError)
    assert "lack selected grants" in str(refused.value) and "object.action.list_folder" in str(refused.value)


def test_a_selection_with_its_grants_narrows_as_before():
    narrowed = narrow_named_service_config(
        config=DOCS, resource="named_services", selected={"docs": ["object.action.list_folder"]},
        grants=["named_services:use", "drive:read"],
    )
    assert list(narrowed["namespaces"]["docs"]["tools"]["action"]["operations"]) == ["object.action.list_folder"]


@pytest.mark.asyncio
async def test_stage_signs_each_action_and_its_missing_grants_on_the_named_refusal():
    planner = _Planner({"ok": False, "error": SELECTION, "status": 409, "operations": list(reversed(ROWS))})
    operation, _, store = _operation(planner=planner)
    request = _request()
    assert _verified(await operation.answer(request), request) == {
        "kind": "refused", "code": SELECTION, "status": 409, "operations": ROWS}
    assert store.staged == []


@pytest.mark.asyncio
async def test_without_rows_the_refusal_is_named_with_its_fixed_shape():
    operation, _, _ = _operation(planner=_Planner({"ok": False, "error": SELECTION, "status": 409}))
    request = _request()
    assert _verified(await operation.answer(request), request) == {
        "kind": "refused", "code": SELECTION, "status": 409}


@pytest.mark.asyncio
async def test_no_other_refusal_ever_carries_actions():
    operation, _, _ = _operation(planner=_Planner({"ok": False, "error": "delegated_access_grants_not_delegable",
                                                  "status": 409, "operations": ROWS}))
    request = _request()
    assert _verified(await operation.answer(request), request) == {
        "kind": "refused", "code": "delegated_access_grants_not_delegable", "status": 409}
    assert version.CardVersionRefused("card_changed", operations=ROWS).operations == ()


def test_only_plain_names_are_sent_sorted_distinct_and_bounded():
    assert version.refused_operations([
        {"namespace": "docs", "operation": "b", "grants": ["y:read", "x:read", "bad name"]},
        {"namespace": "docs", "operation": "a", "grants": ["z:read"]},
        {"namespace": "docs", "operation": "a", "grants": ["other:read"]},   # duplicate action: first kept
        {"namespace": "docs", "operation": "c", "grants": []},               # nothing it lacks: left out
        {"namespace": "do cs", "operation": "d", "grants": ["z:read"]},       # not a plain name: left out
        {"namespace": "docs", "operation": "e\n", "grants": ["z:read"]},
        "docs:a", None, 7,
    ]) == (
        {"namespace": "docs", "operation": "a", "grants": ["z:read"]},
        {"namespace": "docs", "operation": "b", "grants": ["x:read", "y:read"]},
    )
    assert version.refused_operations(None) == () and version.refused_operations({"a": 1}) == ()
    many = [{"namespace": "docs", "operation": f"op{index:03d}", "grants": ["g:read"]} for index in range(100)]
    assert len(version.refused_operations(many)) == version.MAX_REFUSED_OPERATIONS


class _RefusingHost(_Host):
    async def _resolve_card_authority(self, **kwargs):
        return SimpleNamespace(error={"error": SELECTION, "message": "...", "operations": ROWS}, revoke=False)


def test_the_planner_keeps_the_actions():
    with pytest.raises(NamedServiceOperationsRefused) as refused:
        _plan(_RefusingHost(), governed=None)
    assert refused.value.reason == SELECTION and str(refused.value) == SELECTION
    assert refused.value.operations == ROWS


def test_the_lifecycle_plan_hands_the_actions_to_stage():
    from connection_hub.delegated_credentials import card_lifecycle_plan

    source = inspect.getsource(card_lifecycle_plan)
    assert 'if getattr(exc, "operations", None):' in source
    assert 'refused["operations"] = list(exc.operations)' in source


def test_the_resolver_returns_the_actions_with_the_refusal():
    from connection_hub.delegated_credentials import automation_access

    source = inspect.getsource(automation_access)
    assert "if isinstance(exc, NamedServiceOperationsLackGrants):" in source
    assert '{"namespace": namespace, "operation": operation, "grants": list(grants)}' in source
