"""W941: a project Control (P) Save is bounded per resource by the saver's own proof.

The managing application names the resources its delegable grants bound
(``governed_resources``, its own). Every other resource of P is bounded by the
saving person's own admission-time authority, from a Hub-signed proof typed to
the project Control (v2 ``target_kind``) and covering the whole P edit. Without
a proof an addition there is refused ``card_grantor_inventory_unavailable``;
keeping or removing what P carries is never refused.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.grantor_authority_proof import (
    AGENT_CARD, PROJECT_CONTROL, GrantorProofError, edit_of, issue_grantor_proof, verify_grantor_proof,
)
from connection_hub.delegated_credentials.managed_card_selection_plan import (
    _verified_saver_authority, build_managed_card_selection_update,
)
from service_foundation.coordination.durable_decision_log import DecisionRefused

SECRET = "k" * 40
SAVER = {"user_id": "user:admin", "roles": ["kdcube:role:registered", "docs:editor"], "permissions": []}
NOW = 1_800_000_000


def _proof(*, kind=PROJECT_CONTROL, subject="user:admin", access_id="p-1", revision=4, request_id="save-1",
           edit=None, user=SAVER):
    return issue_grantor_proof(secret=SECRET, user=user, grantor_subject=subject, access_id=access_id,
                               original_revision=revision, request_id=request_id, edit=edit or {},
                               now=NOW, ttl_seconds=60, target_kind=kind)


def test_a_proof_names_its_card_kind_and_is_never_read_as_the_other() -> None:
    token = _proof(kind=AGENT_CARD)
    with pytest.raises(GrantorProofError):
        verify_grantor_proof(token, secret=SECRET, now=NOW + 1, grantor_subject="user:admin", access_id="p-1",
                             original_revision=4, request_id="save-1", edit={}, target_kind=PROJECT_CONTROL)
    verified = verify_grantor_proof(token, secret=SECRET, now=NOW + 1, grantor_subject="user:admin",
                                    access_id="p-1", original_revision=4, request_id="save-1", edit={},
                                    target_kind=AGENT_CARD)
    assert verified.target_kind == AGENT_CARD


def test_a_project_control_proof_covers_the_whole_p_edit() -> None:
    body = {"selection": {"resource_grants": {}}, "composition_mode": "or", "external_mcp_family": "on",
            "a2a_family": "off", "label": "P", "unrelated": 1}
    assert edit_of(body, target_kind=PROJECT_CONTROL) == {key: body[key] for key in (
        "selection", "composition_mode", "external_mcp_family", "a2a_family", "label")}
    assert "composition_mode" not in edit_of(body), "an agent Card edit keeps its own fields"
    token = _proof(edit=edit_of(body, target_kind=PROJECT_CONTROL))
    changed = edit_of({**body, "composition_mode": "and"}, target_kind=PROJECT_CONTROL)
    with pytest.raises(GrantorProofError):
        verify_grantor_proof(token, secret=SECRET, now=NOW + 1, grantor_subject="user:admin", access_id="p-1",
                             original_revision=4, request_id="save-1", edit=changed, target_kind=PROJECT_CONTROL)


def test_the_saver_proof_attests_the_saver_and_only_them() -> None:
    original = SimpleNamespace(access_id="p-1", card_revision=4, grantor_subject="user:creator")
    host = SimpleNamespace(_grantor_authority_secret=SECRET)
    token = _proof()
    identity = _verified_saver_authority(host, original, token, edit={}, actor_subject="user:admin",
                                         request_id="save-1", now=NOW + 1)
    assert identity["user_id"] == "user:admin" and "docs:editor" in identity["roles"]
    assert _verified_saver_authority(host, original, None, edit={}, actor_subject="user:admin",
                                     request_id="save-1", now=NOW + 1) is None
    with pytest.raises(DecisionRefused, match="card_grantor_proof_invalid"):
        _verified_saver_authority(host, original, token, edit={}, actor_subject="user:someone-else",
                                  request_id="save-1", now=NOW + 1)


def test_a_session_cannot_get_a_proof_for_another_person() -> None:
    with pytest.raises(GrantorProofError):
        _proof(subject="user:creator")  # the session is user:admin


class _Captured(Exception):
    pass


class _Host:
    _grantor_authority_secret = SECRET

    def __init__(self):
        self.bound = "unset"

    async def _catalog_config(self, active, *, owner_subject):
        return SimpleNamespace(resources=())

    async def _resolve_card_authority(self, **kwargs):
        self.bound = kwargs.get("_grantor_bound")
        raise _Captured()


def _plan(host, *, governed=None, proof=None, kind="project_control"):
    from connection_hub.delegated_credentials.managed_card_selection_plan import OPERATIONS
    from connection_hub.delegated_credentials.project_authorization import (
        ProjectAuthorizationDecision, ProjectAuthorizationRequest,
    )
    from test_w638_managed_card_selection_plan import PROJECT, project_control

    original = project_control()
    request = ProjectAuthorizationRequest.build(actor_subject="user:admin", project_ref=PROJECT,
        target_subject=original.access_id, operation=OPERATIONS["project_control"], request_id="save-1")
    decision = ProjectAuthorizationDecision.allow(request, delegable_grants=("work:admin",))
    token = proof(original) if callable(proof) else proof
    return asyncio.run(build_managed_card_selection_update(
        host, original=original, kind=kind, selection={}, active=SimpleNamespace(version=original.catalog_version),
        decision=decision, project_ref=PROJECT, actor_subject="user:admin", request_id="save-1", now=NOW + 1,
        grantor_proof=token, grantor_edit={"selection": {}}, governed_resources=governed))


def test_with_governed_resources_the_planner_splits_by_the_savers_proof() -> None:
    host = _Host()
    with pytest.raises(_Captured):
        _plan(host, governed=["problem_board"],
              proof=lambda original: _proof(access_id=original.access_id, revision=original.card_revision,
                                            edit={"selection": {}}))
    assert host.bound["governed"] == ("problem_board",)
    assert host.bound["authority"]["user_id"] == "user:admin"

    unproven = _Host()
    with pytest.raises(_Captured):
        _plan(unproven, governed=["problem_board"])
    assert unproven.bound == {"governed": ("problem_board",), "authority": None}, (
        "no proof: every non-governed addition is refused card_grantor_inventory_unavailable")


def test_without_governed_resources_a_p_save_is_bounded_as_before() -> None:
    host = _Host()
    with pytest.raises(_Captured):
        _plan(host)
    assert host.bound is None
    with pytest.raises(DecisionRefused, match="card_plan_selection_invalid"):
        _plan(_Host(), proof="x.y")  # a proof without governed resources is not admitted


def test_governed_resources_are_admitted_on_a_project_control_only() -> None:
    with pytest.raises(DecisionRefused, match="card_plan_"):
        _plan(_Host(), governed=["problem_board"], kind="invitation_control")
    for bad in ("problem_board", [""], [" x"]):
        with pytest.raises(DecisionRefused, match="card_plan_selection_invalid"):
            _plan(_Host(), governed=bad)


def test_the_lifecycle_plan_admits_a_proof_on_the_project_controls_own_edit() -> None:
    import inspect

    from connection_hub.delegated_credentials import card_lifecycle_plan

    source = inspect.getsource(card_lifecycle_plan)
    assert '"grantor_proof" in raw and raw.get("kind") not in {"reselect_agent_card",' in source
    assert "PROJECT_CONTROL if raw.get(\"kind\") == \"reselect_project_control\"" in source


def test_the_hub_issues_a_project_control_proof_only_when_its_setting_is_on() -> None:
    # Review F2: the named setting connections.delegated_credentials.control_saver_proof, default off.
    import time

    from connection_hub.delegated_credentials.automation_access import AutomationAccessService
    from connection_hub.delegated_credentials.oauth.config import oauth_delegated_config_from_connections

    connections = {"delegated_credentials": {"oauth": {"enabled": True, "capabilities": [], "resources": []}}}
    record = SimpleNamespace(access_id="p-1", grantor_subject="user:creator")

    def service(**kwargs):
        return AutomationAccessService(redis=object(), tenant="t", project="p",
                                       config=oauth_delegated_config_from_connections(connections),
                                       grantor_authority_secret=SECRET, **kwargs)

    def issue(svc):
        return svc._owner_authority_proof(SAVER, record=record, kind="project_control", revision=4,
                                          request_id="save-1", edit={})

    assert issue(service()) is None, "off by default: an older board refuses a P Save carrying a proof"
    for value in ("true", 1, "yes", None):
        assert issue(service(control_saver_proof=value)) is None, value
    token = issue(service(control_saver_proof=True))
    verified = verify_grantor_proof(token, secret=SECRET, now=int(time.time()), grantor_subject="user:admin",
                                    access_id="p-1", original_revision=4, request_id="save-1", edit={},
                                    target_kind=PROJECT_CONTROL)
    assert verified.grantor_subject == "user:admin", "the saver, not the Control's storage owner"


def test_the_editor_catalog_offers_a_non_admin_no_admin_only_service() -> None:
    # The operator (23:36Z): "catalog does not show to non admin the admin services". The editor's rows come
    # from resource_options, bounded by the person's own delegable set: the same set a P Save's proof bounds.
    from connection_hub.delegated_credentials.automation_access import AutomationAccessService
    from connection_hub.delegated_credentials.oauth.config import oauth_delegated_config_from_connections

    connections = {"delegated_credentials": {"oauth": {
        "enabled": True,
        "capabilities": [
            {"grant": "docs:read", "label": "Read docs", "delegable_roles": ["kdcube:role:registered"]},
            {"grant": "secrets:read", "label": "Read secrets", "delegable_roles": ["kdcube:role:super-admin"]},
        ],
        "resources": [
            {"resource": "docs", "label": "Docs", "tools": {"docs.search": {"label": "Search", "grants": ["docs:read"]}}},
            {"resource": "secrets", "label": "Secrets", "admin_only": True,
             "tools": {"secrets.get": {"label": "Get", "grants": ["secrets:read"]}}},
        ],
    }}}

    class _Resolver:
        async def resolve_active(self):
            return SimpleNamespace(version="catalog-1", connections=connections)

    service = AutomationAccessService(redis=object(), tenant="t", project="p",
                                      config=oauth_delegated_config_from_connections(connections),
                                      catalog_resolver=_Resolver())
    member = {"user_id": "user:member", "roles": ["kdcube:role:registered"], "permissions": []}
    admin = {"user_id": "user:root", "roles": ["kdcube:role:super-admin", "kdcube:role:registered"],
             "permissions": []}

    member_rows = {row["resource"] for row in asyncio.run(service.resource_options(member))}
    admin_rows = {row["resource"] for row in asyncio.run(service.resource_options(admin))}

    assert "docs" in member_rows and "secrets" not in member_rows
    assert {"docs", "secrets"} <= admin_rows


def test_governed_resources_travel_only_on_the_project_controls_own_edit() -> None:
    # Note 1b (D4): the lifecycle plan admits the field only on reselect_project_control, as a non-empty list
    # of resource ids, and hands it to the planner; an older Hub refuses the unknown key (C1).
    import inspect

    from connection_hub.delegated_credentials import card_lifecycle_plan

    source = inspect.getsource(card_lifecycle_plan)
    assert '"governed_resources" in raw and (raw.get("kind") != "reselect_project_control"' in source
    assert "governed_resources=raw.get(\"governed_resources\")" in source


def test_governed_resources_are_part_of_both_digests() -> None:
    # Note 1b (D3): PLAN and STAGE read the same update, and the STAGE digest keeps the field while it strips
    # only the proof (an attestation of the edit, not part of it).
    from connection_hub.delegated_credentials.cards.participant_card_version import REQUEST_FIELDS, stage_digest

    def request(update):
        base = {name: None for name in REQUEST_FIELDS}
        base.update({"request_id": "r", "at": "2026-10-24T00:00:00Z", "catalog": {}, "actor_subject": "a",
                     "actor_kind": "caller", "delegable_grants": [], "project_control": None, "creations": [],
                     "updates": [update]})
        return base

    p_edit = {"kind": "reselect_project_control", "selection": {}}
    governed = {**p_edit, "governed_resources": ["problem_board"]}
    assert stage_digest(request(governed)) != stage_digest(request(p_edit))
    assert stage_digest(request({**governed, "grantor_proof": "a.b"})) == stage_digest(request(governed))


@pytest.mark.parametrize("case", ["governed_widening", "non_governed_with_proof", "non_governed_without_proof"])
def test_plan_and_stage_give_the_same_split(case) -> None:
    # claude-e-app's C2: STAGE rebuilds each step's decision from its own request (stage_authorization) and
    # hands the planner the very same update, so the split it computes is PLAN's. Cases (a)-(c) of C6.
    from connection_hub.delegated_credentials.cards.participant_card_version import stage_authorization
    from connection_hub.delegated_credentials.managed_card_selection_plan import OPERATIONS
    from connection_hub.delegated_credentials.project_authorization import (
        ProjectAuthorizationDecision, ProjectAuthorizationRequest,
    )
    from test_w638_managed_card_selection_plan import PROJECT, project_control

    original = project_control()
    governed = ["problem_board"]
    edit = {"selection": {}}
    proof = None if case == "non_governed_without_proof" else _proof(
        access_id=original.access_id, revision=original.card_revision, request_id="save-1", edit=edit)
    update = {"kind": "reselect_project_control", "target_subject": original.access_id,
              "access_id": original.access_id, "subject_hash": "h" * 64, "original_revision": original.card_revision,
              "selection": {}, "governed_resources": governed, **({"grantor_proof": proof} if proof else {})}

    def split(decision):
        host = _Host()
        with pytest.raises(_Captured):
            asyncio.run(build_managed_card_selection_update(
                host, original=original, kind="project_control", selection=update["selection"],
                active=SimpleNamespace(version=original.catalog_version), decision=decision, project_ref=PROJECT,
                actor_subject="user:admin", request_id="save-1", now=NOW + 1, grantor_proof=update.get("grantor_proof"),
                grantor_edit=edit, governed_resources=update["governed_resources"]))
        return host.bound

    plan_request = ProjectAuthorizationRequest.build(actor_subject="user:admin", project_ref=PROJECT,
        target_subject=original.access_id, operation=OPERATIONS["project_control"], request_id="save-1")
    plan_bound = split(ProjectAuthorizationDecision.allow(plan_request, delegable_grants=("work:admin",)))

    data = {"actor_subject": "user:admin", "scope": PROJECT, "request_id": "save-1", "at": "2026-10-24T00:00:00Z",
            "delegable_grants": ["work:admin"], "project_control": None, "creations": [], "updates": [update]}
    staged = stage_authorization(data, "d" * 64)
    (_, stage_decision), = staged.decisions
    stage_bound = split(stage_decision)

    assert stage_bound == plan_bound
    assert plan_bound["governed"] == ("problem_board",)
    if case == "non_governed_without_proof":
        assert plan_bound["authority"] is None, "a non-governed addition is refused card_grantor_inventory_unavailable"
    else:
        assert plan_bound["authority"]["user_id"] == "user:admin"



def test_a_changed_governed_resources_after_authorization_is_refused() -> None:
    # Note 1b, D2: the board authorizes the PLAN request, whose digest covers every update, governed_resources
    # included; a decision for one value never validates for a request carrying another.
    from connection_hub.delegated_credentials.cards.lifecycle_plan_operation import (
        REQUEST_SCHEMA, plan_request_digest, plan_steps,
    )
    from connection_hub.delegated_credentials.project_authorization import (
        LifecyclePlanAuthorizationRequest, ProjectAuthorizationDecision, ProjectAuthorizationError,
    )
    from test_w638_managed_card_selection_plan import PROJECT, project_control

    original = project_control()

    def request(governed):
        updates = [{"kind": "reselect_project_control", "target_subject": original.access_id,
                    "access_id": original.access_id, "subject_hash": "h" * 64,
                    "original_revision": original.card_revision, "selection": {}, "governed_resources": governed}]
        data = {"schema": REQUEST_SCHEMA, "request_echo": "e" * 32, "scope": PROJECT, "actor_subject": "user:admin",
                "actor_kind": "caller", "request_id": "save-1", "creations": [], "updates": updates}
        return LifecyclePlanAuthorizationRequest(actor_subject="user:admin", project_ref=PROJECT, request_id="save-1",
                                                 request_digest=plan_request_digest(data), steps=plan_steps([], updates))

    authorized, changed = request(["problem_board"]), request(["problem_board", "docs"])
    assert authorized.request_digest != changed.request_digest
    (step,) = authorized.steps
    decision = ProjectAuthorizationDecision.allow(authorized.step_request(step), delegable_grants=("work:admin",))
    decision.validate_for(authorized.step_request(step))
    (changed_step,) = changed.steps
    with pytest.raises(ProjectAuthorizationError, match="request_digest_mismatch"):
        decision.validate_for(changed.step_request(changed_step))
