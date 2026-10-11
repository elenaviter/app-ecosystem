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
async def test_full_lifecycle_plan_displays_mixed_my_save_without_relaxing_settings_only_contract():
    from dataclasses import replace
    from connection_hub.delegated_credentials.card_lifecycle_plan import (
        build_application_control, build_project_person_control, plan_card_lifecycle,
    )
    from connection_hub.delegated_credentials.controls.project_person import ProjectPersonControlIdentity
    from test_w607_reselect_plan_integration import _Host as PlannerHost

    card = original()
    project = build_application_control(project_ref=PROJECT, holder_subject=ACTOR,
        catalog_version=card.catalog_version, now=123)
    control = build_project_person_control(
        identity=ProjectPersonControlIdentity.build(project_ref=PROJECT, target_subject=ACTOR),
        catalog_version=card.catalog_version, actor_subject=ACTOR, request_id="parent-fixture",
        parent=project, now=123)
    host = PlannerHost(project, control, card)
    host.catalog = replace(host.catalog, version=card.catalog_version)
    properties = copy.deepcopy(card.to_dict()["properties"])
    properties[EMAIL]["email"] = "new@example.test"
    data = stage(card, {})
    data["updates"][0].update(kind="reselect", selection={"properties": properties,
        "resource_operations": {"synthetic": ["get", "write"]}}, label="Whole renamed Card",
        expected_catalog_version=card.catalog_version, invocation_modes={"synthetic": {"write": "once"}})
    authorization = stage_authorization(data, DIGEST)
    result = await plan_card_lifecycle(host, project_ref=PROJECT, creations=[], updates=data["updates"],
        actor_subject=ACTOR, actor_kind="caller", request_id=data["request_id"], authorization=authorization)
    assert result["ok"] is True, result
    [entry] = result["plan"]["display"]
    assert entry["before"]["label"] == card.label
    assert entry["after"]["label"] == "Whole renamed Card"
    assert entry["after"]["resource_operations"] == {"synthetic": ["get", "write"]}
    assert entry["after"]["properties"][EMAIL] == {
        "email": "new@example.test", "set_by": ACTOR,
        "set_at": int(datetime.fromisoformat(AT).timestamp())}
    assert "kept" not in entry["after"]["properties"] and host.writes == 0

    # A display opt-in does not turn legacy properties-only changes into
    # mixed Saves, and it still rejects undisplayed/non-settings changes.
    from connection_hub.delegated_credentials.plan_display import plan_display
    from connection_hub.delegated_credentials.cards.store import subject_hash_for
    key = (subject_hash_for(ACTOR), card.access_id)
    value = result["plan"]["candidate_value"]
    with pytest.raises(DecisionRefused, match="card_plan_display_person_settings_invalid"):
        plan_display(value, {key: card.to_dict()}, person_settings={key: (PROJECT, ACTOR)})
    changed = copy.deepcopy(value)
    changed["cards"][0]["candidate"]["properties"]["kept"] = {"value": False}
    with pytest.raises(DecisionRefused, match="card_plan_display_person_settings_invalid"):
        plan_display(changed, {key: card.to_dict()}, person_settings={key: (PROJECT, ACTOR)},
                     whole_card_settings=frozenset({key}))


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
