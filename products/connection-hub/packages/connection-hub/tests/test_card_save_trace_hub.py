"""The Card save is traced on the Hub side: one trace id per save, every hop, fixed codes only.

Operator, 10 Oct 2026 ~11:40 Berlin: "logs. we must have logs that trace request end to end".
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from connection_hub import card_save_trace
from connection_hub.delegated_credentials.managed_card_edit_forward import ManagedCardEditError
from connection_hub.delegated_credentials.project_authorization import ProjectAuthorizationError
from test_w578_peer_plan_authorization import _Board, _decisions, _port, _request
from test_w638_managed_card_edit_forward import Host, forward, service


def _id(value):
    return card_save_trace.safe_id(value)


def _lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("card-save trace=")]


def test_the_forward_to_the_project_logs_entry_and_ok_under_the_saves_request_id(caplog):
    caplog.set_level(logging.INFO, logger=card_save_trace.LOGGER.name)
    forward(service(Host()))
    lines = _lines(caplog)
    assert [line.split(" outcome=")[1].split(" ")[0] for line in lines] == ["entry", "ok"]
    assert all(line.startswith(f"card-save trace={_id('edit-1')} hop=hub.card_edit_forward ") for line in lines)


def test_a_lost_project_answer_logs_the_hubs_fixed_code(caplog):
    caplog.set_level(logging.INFO, logger=card_save_trace.LOGGER.name)
    forward(service(Host(fail=True)))
    assert "outcome=refused code=managed_card_edit_outcome_unknown " in _lines(caplog)[-1]


@pytest.mark.asyncio
async def test_the_plan_authorize_callback_is_traced_by_the_plan_id_and_names_its_refusal(caplog):
    caplog.set_level(logging.INFO, logger=card_save_trace.LOGGER.name)
    request = _request(request_id="card-edit-plan:abc")
    envelope = await _port(_Board(lambda body: {"ok": True, "decisions": _decisions(request)})
                           ).authorize_lifecycle_plan(request)
    assert envelope.allowed
    with pytest.raises(ProjectAuthorizationError, match="card_plan_authorization_refused"):
        await _port(_Board(lambda body: {"ok": False, "error": {"code": "work_x"}})).authorize_lifecycle_plan(request)
    lines = _lines(caplog)
    assert [line.split(" outcome=")[1].split(" ")[0] for line in lines] == ["entry", "ok", "entry", "refused"]
    assert all(line.startswith(f"card-save trace={_id('card-edit-plan:abc')} hop=hub.plan_authorize_callback ")
               for line in lines)
    assert "card-edit-plan:abc" not in caplog.text
    assert "code=card_plan_authorization_refused " in lines[-1]


def test_hops_inside_one_save_keep_the_outer_trace_id(caplog):
    caplog.set_level(logging.INFO, logger=card_save_trace.LOGGER.name)
    with card_save_trace.trace_scope("ui-request-7"):
        forward(service(Host()))
    assert all(line.startswith(f"card-save trace={_id('ui-request-7')} ") for line in _lines(caplog))


@pytest.mark.parametrize("value", ["alice", "free text with a name", "eyJhbGciOi.token.value", "work_secret_token_x",
                                   "card_secret_token_x", None])
def test_only_hub_owned_fixed_codes_are_logged(value):
    shown = card_save_trace.fixed_code(value)
    assert shown == ("-" if value is None else "other")
    assert card_save_trace.fixed_code("card_plan_authorization_refused") == "card_plan_authorization_refused"
    assert card_save_trace.fixed_code("managed_card_edit_outcome_unknown") == "managed_card_edit_outcome_unknown"


def test_a_plan_answer_refusal_reads_its_signed_result_code():
    answer = {"ok": False, "plan_answer": {"result": {"kind": "refused", "code": "card_participant_unavailable"}}}
    assert card_save_trace.answer_outcome(answer) == ("refused", "card_participant_unavailable")
    assert card_save_trace.answer_outcome({"ok": False, "error": {"code": "card_plan_empty"}}) == (
        "refused", "card_plan_empty")
    assert card_save_trace.answer_outcome({"ok": True}) == ("ok", None)


def test_an_unsafe_request_id_is_a_digest(caplog):
    caplog.set_level(logging.INFO, logger=card_save_trace.LOGGER.name)
    with card_save_trace.trace_scope("has a name@example in it"):
        card_save_trace.hop("hub.person_control_update", "entry")
    line = _lines(caplog)[-1]
    assert line.startswith("card-save trace=h:") and "name@example" not in line


def test_errors_carry_reason_attributes_the_trace_reads():
    assert ManagedCardEditError("managed_card_edit_answer_invalid", 502).reason == "managed_card_edit_answer_invalid"
    assert ProjectAuthorizationError("card_plan_authorization_refused").reason == "card_plan_authorization_refused"


@pytest.mark.parametrize("request_id", ["eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.sig", "ghp_0123456789abcdefABCDEF", "edit-9"])
def test_a_token_shaped_or_plain_request_id_never_appears(caplog, request_id):
    caplog.set_level(logging.INFO, logger=card_save_trace.LOGGER.name)
    with card_save_trace.trace_scope(request_id):
        card_save_trace.hop("hub.person_control_update", "entry", plan=request_id)
    line = _lines(caplog)[-1]
    assert line.startswith(f"card-save trace={_id(request_id)} ") and f"plan={_id(request_id)}" in line
    assert request_id not in caplog.text


def test_the_committed_hub_code_set_covers_the_card_save_modules():
    import re
    from pathlib import Path
    from connection_hub import card_save_codes
    root = Path(card_save_codes.__file__).parent / "delegated_credentials"
    found = set()
    for name in ("managed_card_edit_forward.py", "project_authorization.py", "cards/lifecycle_plan_authorization.py",
                 "cards/lifecycle_plan_operation.py", "card_lifecycle_plan.py", "agent_lifecycle_plan.py",
                 "existing_card_selection_plan.py"):
        text = (root / name).read_text(encoding="utf-8")
        found.update(code for code in re.findall(
            r'"((?:card|agent|project|lifecycle|control|managed_card_edit|delegated|catalog|resource)_[a-z0-9_]+)"', text)
            if not code.endswith("_"))
    assert found - card_save_codes.CARD_SAVE_CODES == set()
