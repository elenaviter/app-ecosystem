"""The GitHub token route authenticates the agent's Card for identity only (W371).

The first real push (2026-09-28, 15:48Z) was refused before the route ran:
the platform checks every application operation called with a delegated
``Authorization`` bearer against the Card's selected application operations,
and no catalog offers ``project_agent_github_token_issue``. The Card bearer
now rides its own header; the platform sees no delegated bearer, and the
route verifies the Card itself: live, its grantor and access id known, with
no resource or operation grant. "Use GitHub" (the project host's decision)
stays the only thing a person grants.

Real guard code throughout: the platform's application-operation check and
its delegated bearer verification and identity boundary, with the bearer
authenticator and the grant store faked as the platform's own guard tests fake
them, and the live-Card restoration standing in only to name the Card.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from connection_hub.delegated_credentials.cards.cache import DelegatedCardRuntimeCache
from connection_hub.delegated_credentials.cards.reconcile import CardProjectionEpochGate
from kdcube_ai_app.apps.chat.sdk.application_operations import (
    APPLICATION_OPERATION_POLICY_PROPERTY,
    application_operation_policy,
    application_operation_ref,
)
from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.oauth import (
    surface_guard,
)
from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.oauth.tests.helpers import (
    bind_delegated_catalog,
)
from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import (
    load_dynamic_module_for_path,
)

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
BOARD_RESOURCE = "http://testserver/api/integrations/bundles/home/demo/problem-board@1-0/public/mcp/problem_board"
GRANTOR = "a1b2c3d4-5e6f-7a8b-9c0d-1e2f3a4b5c6d"
BOARD_OPERATION = application_operation_ref(application_id="problem-board@1-0", operation_id="work.receive")
BOARD_AUTHORIZE_OPERATION = application_operation_ref(
    application_id="problem-board@1-0", operation_id="project_agent_github_authorize"
)
ISSUE_OPERATION = application_operation_ref(
    application_id="connection-hub@1-0", operation_id="project_agent_github_token_issue"
)


def _card_bearer_module():
    _name, module = load_dynamic_module_for_path(BUNDLE_ROOT / "surfaces" / "card_bearer.py")
    return module


@pytest.fixture(autouse=True)
def _projections_swept(monkeypatch):
    """Card projections already swept, as after startup (the platform guard tests do the same)."""

    async def _ready(self):
        return True

    async def _read_in_current_run(self, access_id):
        return True, await self.read(access_id)

    monkeypatch.setattr(CardProjectionEpochGate, "is_ready", _ready)
    monkeypatch.setattr(DelegatedCardRuntimeCache, "read_in_current_run", _read_in_current_run)


class _GrantStore:
    def __init__(self, record):
        self.record = record

    async def get_access_grant_record(self, access_token: str):
        return self.record


def _worker_card() -> dict:
    """A worker Card as the board makes it: the Problem Board resource and board operations only."""

    grants = {BOARD_RESOURCE: ["work:relay"], "*": ["kdcube:role:registered"]}
    return {
        "operations": [BOARD_OPERATION],
        "properties": {APPLICATION_OPERATION_POLICY_PROPERTY: application_operation_policy()},
        "credential": {
            "schema": "kdcube.credential.v1",
            "credential_kind": "delegated_client_access",
            "issuer_authority_id": "delegated_client",
            "issuer_authenticator_id": "delegated_client.bearer",
            "subject": f"integration:agent-card-1:{GRANTOR}",
            "audience": "kdcube:delegated_client",
            "attrs": {
                "scopes": ["work:relay", "kdcube:role:registered"],
                "resource_grants": grants,
                "resource_operations": {"*": [BOARD_OPERATION]},
                "grantor_subject": GRANTOR,
                "client_id": "agent-card-1",
                "identity_scope": "grantor",
            },
        },
    }


def _client(monkeypatch) -> TestClient:
    async def fake_authenticate(token: str):
        if token != "card-bearer":
            return None
        return {"sub": f"integration:agent-card-1:{GRANTOR}", "roles": ["kdcube:role:delegated-client"], "permissions": []}

    monkeypatch.setattr(surface_guard, "_authenticate_delegated_client_access_token", fake_authenticate)

    real_live_grant_record = surface_guard._live_grant_record  # noqa: SLF001

    async def live_grant_record(request, grant_record):
        # The live Card restoration (the platform's own, tested there) names
        # the Card the bearer belongs to; everything else runs as it is.
        restored = await real_live_grant_record(request, grant_record)
        return {**restored, "registry_access_id": "agent-card-1"} if isinstance(restored, dict) else restored

    monkeypatch.setattr(surface_guard, "_live_grant_record", live_grant_record)
    module = _card_bearer_module()
    app = FastAPI()
    app.state.oauth_grant_store = _GrantStore(_worker_card())
    app.state.oauth_delegated_config = {"tenant": "home", "project": "demo"}
    bind_delegated_catalog(
        app,
        {
            "delegated_credentials": {
                "oauth": {
                    "enabled": True,
                    "resources": [
                        {"resource": BOARD_RESOURCE, "grants": ["work:relay"]},
                        {"resource": "*", "grants": ["kdcube:role:registered"]},
                    ],
                }
            }
        },
    )

    @app.post("/issue")
    async def issue(request: Request):
        # The platform's order: the application-operation check, then the app's route.
        denial = await surface_guard.authorize_delegated_application_operation_request(
            request=request, operation=ISSUE_OPERATION, method="POST"
        )
        if denial is not None:
            return denial
        token = module.card_bearer(request)
        if not token:
            return JSONResponse({"ok": False, "error": "agent_credential_missing"}, status_code=401)
        denial = await module.authenticate_card_bearer(request, token)
        if denial is not None:
            return denial
        view = surface_guard.DelegatedCredentialView.from_request(request)
        return JSONResponse(
            {"ok": True, "grantor": view.grantor_user_id, "access_id": view.registry_access_id, "client_id": view.client_id}
        )

    @app.post("/issue-then-ask-the-board")
    async def issue_then_ask(request: Request):
        # The route's order: authenticate the Card, then ask the board as a
        # peer call inside this same request (the platform runs the
        # application-operation check on that call too).
        denial = await module.authenticate_card_bearer(request, module.card_bearer(request))
        if denial is not None:
            return denial
        under_card = await surface_guard.authorize_delegated_application_operation_request(
            request=request, operation=BOARD_AUTHORIZE_OPERATION, method="POST"
        )
        module.forget_card_bearer(request)
        as_hub = await surface_guard.authorize_delegated_application_operation_request(
            request=request, operation=BOARD_AUTHORIZE_OPERATION, method="POST"
        )
        return JSONResponse(
            {
                "under_card": getattr(under_card, "status_code", 0),
                "as_hub": getattr(as_hub, "status_code", 0),
                "credential_left": getattr(request.state, "delegated_credential", None) is not None,
            }
        )

    return TestClient(app)


def test_the_card_in_authorization_is_refused_by_the_application_operation_check(monkeypatch):
    """The first real push, reproduced: no Card can select this route."""

    response = _client(monkeypatch).post("/issue", headers={"Authorization": "Bearer card-bearer"})

    assert response.status_code == 403
    assert "project_agent_github_token_issue" in response.json()["error_description"]


def test_the_card_in_its_own_header_is_authenticated_for_identity_with_only_the_board_resource(monkeypatch):
    response = _client(monkeypatch).post("/issue", headers={"X-Connection-Hub-Card-Bearer": "card-bearer"})

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "grantor": GRANTOR, "access_id": "agent-card-1", "client_id": "agent-card-1"}


def test_a_bearer_prefix_is_accepted_and_a_dead_bearer_is_refused(monkeypatch):
    client = _client(monkeypatch)

    prefixed = client.post("/issue", headers={"X-Connection-Hub-Card-Bearer": "Bearer card-bearer"})
    dead = client.post("/issue", headers={"X-Connection-Hub-Card-Bearer": "not-a-card"})
    missing = client.post("/issue")

    assert prefixed.status_code == 200
    assert dead.status_code == 401
    assert missing.status_code == 401


def test_the_board_is_asked_as_connection_hub_not_under_the_agents_card(monkeypatch):
    """Second blocker of the first push (16:01Z): the peer call to the board ran
    under the agent's Card and was refused for want of a bearer. Once the Card's
    facts are read, the route drops them, and the call passes the platform check;
    the board admits it by the peer proof alone."""

    response = _client(monkeypatch).post(
        "/issue-then-ask-the-board", headers={"X-Connection-Hub-Card-Bearer": "card-bearer"}
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"under_card": 401, "as_hub": 0, "credential_left": False}
