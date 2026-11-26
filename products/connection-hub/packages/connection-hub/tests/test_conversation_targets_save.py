"""An owner's own Card that no Control governs may change its conversation applications.

Since W755 the whole-Card Save refused every property change except on a
person's My Card, so "Conversation applications" on an OAuth or automation Card
could no longer be saved (live 2026-11-18, managed_card_edit_fields_unsupported
with the project's wording). The operator's rule for that Save: "any change in
the card simply must work". Only kdcube.conversation_targets may change; every
other property stays as stored, and a managed or Control-bound Card is unchanged.
"""

from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.conversation_target_policy import CONVERSATION_TARGETS_PROPERTY
from connection_hub.delegated_credentials.whole_card_save import WholeCardSaveRefused, checked_properties

APP = "urn:kdcube:app:tenant:project:knowledge@1-0:*"


def card(**properties):
    return SimpleNamespace(properties=dict(properties))


def test_an_own_ungoverned_card_saves_new_conversation_applications():
    stored = card(**{"kdcube.other": "kept"})
    saved = checked_properties(
        stored, {"kdcube.other": "kept", CONVERSATION_TARGETS_PROPERTY: [APP, "news@2026-05-20-12-05"]},
        own_my=False, own_conversation_targets=True,
    )
    assert saved == {"kdcube.other": "kept", CONVERSATION_TARGETS_PROPERTY: sorted([APP, "news@2026-05-20-12-05"])}
    cleared = checked_properties(
        card(**{CONVERSATION_TARGETS_PROPERTY: [APP]}), {}, own_my=False, own_conversation_targets=True,
    )
    assert cleared == {}


def test_any_other_property_change_is_still_refused():
    with pytest.raises(WholeCardSaveRefused) as refused:
        checked_properties(
            card(**{"kdcube.other": "kept"}),
            {"kdcube.other": "changed", CONVERSATION_TARGETS_PROPERTY: [APP]},
            own_my=False, own_conversation_targets=True,
        )
    assert refused.value.reason == "managed_card_edit_fields_unsupported"
    assert "conversation applications" in refused.value.to_dict()["message"]


@pytest.mark.parametrize("bad", [["*"], [" padded"], ["urn:kdcube:app:broken"], "knowledge@1-0", [7]])
def test_an_invalid_conversation_application_refuses(bad):
    with pytest.raises(WholeCardSaveRefused) as refused:
        checked_properties(card(), {CONVERSATION_TARGETS_PROPERTY: bad}, own_my=False, own_conversation_targets=True)
    assert refused.value.reason == "conversation_targets_invalid"
    assert refused.value.status == 400


def test_a_managed_or_control_bound_card_keeps_refusing_any_property_change():
    with pytest.raises(WholeCardSaveRefused) as refused:
        checked_properties(card(), {CONVERSATION_TARGETS_PROPERTY: [APP]}, own_my=False)
    assert refused.value.reason == "managed_card_edit_fields_unsupported"
