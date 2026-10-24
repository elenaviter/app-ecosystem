"""W941: an agent Card Save is bounded by the project only for the resources the managing application governs.

Once a project's Control P carries another service (Docs, say), every agent Card in that project
must still be able to take that service from its owner. Before this, the Hub counted every resource
P lists as governed by the project, so such an addition was bounded by the application's own
delegable grants (its board grants only) and refused delegated_access_grants_not_delegable for
everyone. The managing application now names its own resources (``governed_resources``); every
other resource is bounded by the owner's proven authority, as W918 intended.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import replace
from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.grantor_authority_proof import AGENT_CARD, issue_grantor_proof
from connection_hub.delegated_credentials.managed_card_selection_plan import (
    OPERATIONS, build_managed_card_selection_update,
)
from connection_hub.delegated_credentials.project_authorization import (
    ProjectAuthorizationDecision, ProjectAuthorizationRequest,
)
from service_foundation.coordination.durable_decision_log import DecisionRefused
from test_w638_managed_card_selection_plan import PROJECT, RESOURCE, agent_card, project_control

SECRET = "k" * 40
NOW = 1_800_000_000
DOCS = "kdcube-services:named_services:docs"
OWNER = {"user_id": "synthetic-owner", "roles": ["kdcube:role:registered"], "permissions": []}


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


def _p_with_docs():
    p = project_control()
    return replace(p, resource_grants={RESOURCE: ("work:admin",), DOCS: ("named_services:use", "docs:read")})


def _plan(host, *, governed=None, with_proof=True, reads=None):
    p = _p_with_docs()
    original = agent_card(p)
    edit = {"selection": {}}
    token = issue_grantor_proof(secret=SECRET, user=OWNER, grantor_subject=original.grantor_subject,
                                access_id=original.access_id, original_revision=original.card_revision,
                                request_id="save-1", edit=edit, now=NOW, ttl_seconds=60,
                                target_kind=AGENT_CARD) if with_proof else None

    async def load_live(subject_hash, access_id):
        if reads is not None:
            reads.append(access_id)
        return p

    request = ProjectAuthorizationRequest.build(actor_subject=original.grantor_subject, project_ref=PROJECT,
        target_subject=original.access_id, operation=OPERATIONS["agent_card"], request_id="save-1")
    decision = ProjectAuthorizationDecision.allow(request, delegable_grants=("work:admin",))
    return asyncio.run(build_managed_card_selection_update(
        host, original=original, kind="agent_card", selection={},
        active=SimpleNamespace(version=original.catalog_version), decision=decision, project_ref=PROJECT,
        actor_subject=original.grantor_subject, request_id="save-1", now=NOW + 1, grantor_proof=token,
        grantor_edit=edit, governed_resources=governed, load_live=load_live))


def test_with_governed_resources_a_service_on_p_is_bounded_by_the_owners_proof():
    # The rehearsal case: P carries Docs; the board names only its own resource, so Docs on the agent Card is
    # bounded by the owner's own authority, not by the board's grants.
    host = _Host()
    with pytest.raises(_Captured):
        _plan(host, governed=[RESOURCE])
    assert host.bound["governed"] == (RESOURCE,)
    assert DOCS not in host.bound["governed"]
    assert host.bound["authority"]["user_id"] == "synthetic-owner"


def test_without_a_proof_the_owners_service_is_refused_not_granted_by_the_board():
    # An admin (or anyone without the owner's proof) still cannot add Docs: no authority means the
    # resolver refuses the addition card_grantor_inventory_unavailable.
    host = _Host()
    with pytest.raises(_Captured):
        _plan(host, governed=[RESOURCE], with_proof=False)
    assert host.bound == {"governed": (RESOURCE,), "authority": None}


def test_without_the_field_every_resource_p_lists_is_governed_as_before():
    # An older board sends no governed_resources: today's behaviour, unchanged.
    host = _Host()
    with pytest.raises(_Captured):
        _plan(host)
    assert host.bound["governed"] == tuple(sorted((RESOURCE, DOCS)))


def test_the_live_p_read_stays_when_the_application_names_its_resources():
    # STAGE fences the agent Card's parent read; naming governed resources never skips it.
    reads: list = []
    with pytest.raises(_Captured):
        _plan(_Host(), governed=[RESOURCE], reads=reads)
    assert reads == [project_control().access_id]


@pytest.mark.parametrize("bad", ["x", [], [""], [" x"], [7]])
def test_malformed_governed_resources_are_refused(bad):
    with pytest.raises(DecisionRefused, match="card_plan_selection_invalid"):
        _plan(_Host(), governed=bad)


def test_the_lifecycle_plan_admits_governed_resources_on_an_agent_cards_own_edit():
    from connection_hub.delegated_credentials import card_lifecycle_plan

    source = inspect.getsource(card_lifecycle_plan)
    assert '"governed_resources" in raw and (raw.get("kind") not in {"reselect_project_control",' in source
    assert '"reselect_agent_card"}' in source
