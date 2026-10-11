"""Mixed permissions/settings in one real candidate builder; no PB/PG qualification."""
import copy
from datetime import datetime
from types import SimpleNamespace

import pytest
from service_foundation.coordination.durable_decision_log import DecisionRefused

from connection_hub.delegated_credentials.cards.participant_card_version import stage_authorization
from connection_hub.delegated_credentials.existing_card_selection_plan import build_existing_card_selection_update
from test_w607_existing_card_selection_plan import _Host
from test_w738_managed_my_card_settings import original, stage, ACTOR, PROJECT, DIGEST, AT, EMAIL


@pytest.mark.asyncio
async def test_permission_rename_and_own_setting_form_one_candidate_with_signed_stage_audit():
    card = original()
    properties = copy.deepcopy(card.to_dict()["properties"])
    properties[EMAIL]["email"] = "new@example.test"
    data = stage(card, {})
    data["updates"][0].update(kind="reselect", selection={"properties": properties,
        "resource_operations": {"synthetic": ["get", "write"]}}, label="Whole renamed Card",
        expected_catalog_version="synthetic-v1", invocation_modes={"synthetic": {"write": "once"}})
    auth = stage_authorization(data, DIGEST)
    built = await build_existing_card_selection_update(_Host(), original=card,
        selection=data["updates"][0]["selection"], label=data["updates"][0]["label"],
        active=SimpleNamespace(version="synthetic-v1"), decision=auth.decision_for("update:0"),
        project_ref=PROJECT, target_subject=ACTOR, actor_subject=ACTOR, request_id=data["request_id"], now=123)
    member = built["member"]
    assert member["original_revision"] == card.card_revision
    candidate = member["candidate"]
    assert candidate["card_revision"] == card.card_revision + 1
    assert candidate["resource_operations"] == {"synthetic": ["get", "write"]}
    assert candidate["label"] == "Whole renamed Card"
    assert candidate["properties"][EMAIL] == {"email": "new@example.test", "set_by": ACTOR,
                                             "set_at": int(datetime.fromisoformat(AT).timestamp())}
    assert candidate["properties"]["kept"] == card.properties["kept"]
    assert candidate["control_card"] == card.to_dict()["control_card"]
    assert card.properties[EMAIL]["email"] == "old@example.test"


@pytest.mark.asyncio
async def test_mixed_settings_refuse_without_real_stage_evidence():
    card = original()
    properties = copy.deepcopy(card.to_dict()["properties"])
    properties[EMAIL]["email"] = "new@example.test"
    data = stage(card, {})
    data["updates"][0].update(kind="reselect", selection={"properties": properties,
        "resource_operations": {"synthetic": ["get", "write"]}})
    auth = stage_authorization(data, DIGEST)
    from dataclasses import replace
    with pytest.raises(DecisionRefused, match="card_plan_person_settings_stage_required"):
        await build_existing_card_selection_update(_Host(), original=card,
            selection=data["updates"][0]["selection"], active=SimpleNamespace(version="synthetic-v1"),
            decision=replace(auth.decision_for("update:0"), evidence={}),
            project_ref=PROJECT, target_subject=ACTOR, actor_subject=ACTOR, request_id=data["request_id"], now=123)
