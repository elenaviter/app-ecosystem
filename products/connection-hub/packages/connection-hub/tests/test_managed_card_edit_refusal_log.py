"""Live 2026-10-09 22:13Z: a refused Control save read only "The project did not save this Card change." and
neither side logged why. The Hub now logs each refusal the project returns: target kind and fixed code only."""
from __future__ import annotations

import logging

import pytest

from connection_hub.delegated_credentials import managed_card_edit_forward as forward

BODY = {"schema": forward.SCHEMA, "request_id": "request-1", "target": {"kind": "person_control"}}


def test_a_project_refusal_is_logged_by_kind_and_code_and_still_raised(caplog):
    caplog.set_level(logging.WARNING)
    with pytest.raises(forward.ManagedCardEditError) as refused:
        forward._outcome({"ok": False, "error": {"code": "card_edit_actor_cards_denied",
                                                  "message": "private detail for the person"},
                          "status": 403}, BODY)
    assert refused.value.reason == "card_edit_actor_cards_denied" and refused.value.status == 403
    assert ("managed card edit refused by the project: kind=person_control code=card_edit_actor_cards_denied "
            "status=403") in caplog.text
    assert "private detail" not in caplog.text


def test_a_refusal_code_that_is_not_a_fixed_code_is_never_logged(caplog):
    caplog.set_level(logging.WARNING)
    with pytest.raises(forward.ManagedCardEditError):
        forward._outcome({"ok": False, "error": "Free text: something secret-ish"}, BODY)
    assert "code=- status=-" in caplog.text and "secret-ish" not in caplog.text


@pytest.mark.parametrize("flat", ["synthetic_private_project_detail", "card_edit_actor_cards_denied"])
def test_a_flat_error_string_is_never_logged_even_when_it_is_spelled_like_a_code(caplog, flat):
    # CodeApp return on #720: lexical shape is not proof of a fixed value-free code.
    caplog.set_level(logging.WARNING)
    with pytest.raises(forward.ManagedCardEditError) as refused:
        forward._outcome({"ok": False, "error": flat, "status": 403}, BODY)
    assert refused.value.reason == flat and refused.value.status == 403  # the refusal itself is unchanged
    assert flat not in caplog.text and "code=- status=403" in caplog.text
