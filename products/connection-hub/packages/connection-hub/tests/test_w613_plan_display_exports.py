"""W613: the owning plan display's public helpers, so a migration reuses them rather than decoding Cards.

``DISPLAY_KINDS`` and ``selection_display`` are the exact names
``plan_display`` itself uses; there is no second decoder.
"""
from __future__ import annotations

import dataclasses

import pytest

from connection_hub.delegated_credentials import plan_display as module
from connection_hub.delegated_credentials.cards.card_group import group_candidate_value, group_member
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.controls.project_invitation import PROJECT_INVITATION_CONTROL_ISSUER_KIND
from connection_hub.delegated_credentials.controls.project_person import PROJECT_PERSON_CONTROL_ISSUER_KIND
from connection_hub.delegated_credentials.plan_display import DISPLAY_KINDS, plan_display, selection_display
from connection_hub.delegated_credentials.project_identity_lifecycle import PROJECT_PERSON_MY_CARD_ISSUER_KIND

from test_w607_existing_card_selection_plan import _cards


def test_the_display_kinds_are_exactly_the_four_displayed_issuers_and_read_only():
    assert dict(DISPLAY_KINDS) == {
        "application": "project_control",
        PROJECT_PERSON_CONTROL_ISSUER_KIND: "person_control",
        PROJECT_PERSON_MY_CARD_ISSUER_KIND: "my_card",
        PROJECT_INVITATION_CONTROL_ISSUER_KIND: "invitation_control",
    }
    with pytest.raises(TypeError):
        DISPLAY_KINDS["other"] = "anything"  # type: ignore[index]


def test_a_selection_display_is_only_the_public_selection_and_a_deep_copy():
    _, control, _ = _cards()
    card = control.to_dict()
    shown = selection_display(card)
    assert set(shown) == {"resource_grants", "resource_operations", "named_service_operations", "account_scope",
                          "parent", "state"}
    assert shown["parent"] == card["control_card"] and shown["state"] == card["state"]
    field = next(iter(shown["parent"]))
    shown["parent"][field] = "changed"
    shown["resource_grants"]["injected"] = ["x"]
    assert card["control_card"][field] != "changed" and "injected" not in card["resource_grants"]


def test_plan_display_shows_every_image_with_the_same_public_helpers():
    project, control, my_card = _cards()
    changed = dataclasses.replace(control, card_revision=control.card_revision + 1,
                                  resource_grants={"service-a": ("work:admin",)})
    value = group_candidate_value([group_member(original=control, candidate=changed, action="update")])
    (entry,) = plan_display(value, {(subject_hash_for(control.grantor_subject), control.access_id): control.to_dict()})
    assert entry["kind"] == DISPLAY_KINDS[control.issuer_kind]
    assert entry["before"] == selection_display(control.to_dict())
    assert entry["after"] == selection_display(changed.to_dict())


def test_only_the_public_names_are_exported():
    assert module.__all__ == ["DISPLAY_KINDS", "plan_display", "selection_display"]
    assert not hasattr(module, "_selection_value") and not hasattr(module, "_DISPLAY_KINDS")
