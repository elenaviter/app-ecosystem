"""The agent GitHub question to the project host is signed, not session-bound (W371).

Connection Hub asks the board ``project_agent_github_authorize`` for an agent
whose Card bearer it authenticated; there is no person session to forward, so
the body carries a peer proof the board verifies with the shared secret.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from connection_hub.project_peer_proof import verify_github_authorize_request
from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import (
    load_dynamic_module_for_path,
)

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
SECRET = "k" * 40
FIELDS = {
    "access_id": "agent-card-1",
    "grantor_subject": "platform-user-2",
    "project_ref": "work:project:quickstart",
    "repository": "example-org/app-ecosystem",
}


def _module():
    _name, module = load_dynamic_module_for_path(BUNDLE_ROOT / "services" / "project_membership.py")
    return module


def _entrypoint(provider: dict) -> SimpleNamespace:
    return SimpleNamespace(bundle_props={"project_membership": {"provider": provider}})


async def _secret(ref: str) -> str:
    return SECRET if ref == "project_membership.peer_proof_secret" else ""


def test_the_question_is_a_signed_public_call_the_board_can_verify() -> None:
    calls = []

    async def caller(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "decision": {"allowed": True, "reason": "", "owner_subject": "platform-user-2"}}

    port = _module().descriptor_github_authorizer(
        _entrypoint({"bundle_id": "problem-board@1-0", "peer_proof_secret_ref": "project_membership.peer_proof_secret"}),
        resolve_secret=_secret,
        caller=caller,
    )

    answer = asyncio.run(port(**FIELDS))

    assert answer["decision"]["allowed"] is True
    call = calls[0]
    assert (call["bundle_id"], call["operation"], call["route"]) == (
        "problem-board@1-0",
        "project_agent_github_authorize",
        "public",
    )
    assert {key: call["data"][key] for key in FIELDS} == FIELDS
    assert verify_github_authorize_request(secret=SECRET, board_bundle_id="problem-board@1-0", body=call["data"]).allowed


def test_without_the_secret_the_question_is_not_asked() -> None:
    calls = []

    async def caller(**kwargs):
        calls.append(kwargs)
        return {"ok": True}

    for provider in (
        {"bundle_id": "problem-board@1-0"},
        {"bundle_id": "problem-board@1-0", "peer_proof_secret_ref": "project_membership.unset"},
    ):
        port = _module().descriptor_github_authorizer(_entrypoint(provider), resolve_secret=_secret, caller=caller)
        answer = asyncio.run(port(**FIELDS))
        assert answer["error"] == "project_github_peer_proof_not_configured"
        assert answer["message"] == "GitHub access waits for the board peer proof."
    assert calls == []


def test_a_board_without_the_operation_says_it_needs_an_update() -> None:
    async def caller(**kwargs):
        raise RuntimeError("Bundle does not support operation project_agent_github_authorize")

    port = _module().descriptor_github_authorizer(
        _entrypoint({"bundle_id": "problem-board@1-0", "peer_proof_secret_ref": "project_membership.peer_proof_secret"}),
        resolve_secret=_secret,
        caller=caller,
    )

    answer = asyncio.run(port(**FIELDS))

    assert answer["error"] == "project_github_board_update_required"


def test_a_guard_refusal_in_front_of_the_board_is_named():
    """A platform guard answers with a response object, not the operation's result (16:01Z)."""

    from fastapi.responses import JSONResponse

    async def caller(**kwargs):
        return JSONResponse({"error": "unauthorized", "reason": "missing_bearer"}, status_code=401)

    port = _module().descriptor_github_authorizer(
        _entrypoint({"bundle_id": "problem-board@1-0", "peer_proof_secret_ref": "project_membership.peer_proof_secret"}),
        resolve_secret=_secret,
        caller=caller,
    )

    answer = asyncio.run(port(**FIELDS))

    assert answer["error"] == "project_github_provider_denied: missing_bearer"
