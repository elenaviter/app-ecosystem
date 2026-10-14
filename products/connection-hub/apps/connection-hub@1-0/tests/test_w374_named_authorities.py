"""W374: the four descriptor ports resolve through named authorities.

A non-empty ``authorities`` block replaces ``project_membership.provider``;
without one, the legacy block is read exactly as before. The two descriptors
below describe the same host, so every port must bind identically.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from connection_hub.authorities import AuthorityConfigError, authorities_from_mapping
from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import (
    load_dynamic_module_for_path,
)

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
BOARD = "problem-board@1-0"
SECRET_REF = "project_membership.peer_proof_secret"

LEGACY = {
    "project_membership": {
        "provider": {
            "bundle_id": BOARD,
            "operation": "project_membership_resolve",
            "agent_card_operation": "project_agent_card_authorize",
            "control_card_operation": "project_control_card_authorize",
            "github_authorize_operation": "project_agent_github_authorize",
            "peer_proof_secret_ref": SECRET_REF,
        },
        "administrative_roles": ["owner", "admin"],
    },
}

NAMED = {
    "authorities": {
        "membership": {"kind": "app_operation", "bundle_id": BOARD, "operation": "project_membership_resolve",
                       "administrative_roles": ["owner", "admin"]},
        "agent_card": {"kind": "app_operation", "bundle_id": BOARD, "operation": "project_agent_card_authorize"},
        "control_card": {"kind": "app_operation", "bundle_id": BOARD, "operation": "project_control_card_authorize"},
        "credential_release": {"kind": "app_operation", "bundle_id": BOARD,
                               "operation": "project_agent_github_authorize", "peer_proof_secret_ref": SECRET_REF},
    },
}


def _module():
    _name, module = load_dynamic_module_for_path(BUNDLE_ROOT / "services" / "project_membership.py")
    return module


def _entrypoint(props: dict) -> SimpleNamespace:
    return SimpleNamespace(bundle_props=props)


async def _secret(ref: str) -> str:
    return "k" * 40 if ref == SECRET_REF else ""


def _bindings(props: dict) -> dict:
    """What each port would call, read from the ports themselves."""

    module = _module()
    entrypoint = _entrypoint(props)
    agent = module.descriptor_agent_card_port(entrypoint)
    control = module.descriptor_control_card_port(entrypoint)
    github = module.descriptor_github_authorizer(entrypoint, resolve_secret=_secret)
    membership = module.descriptor_project_authorization_port(entrypoint)
    resolver = getattr(membership, "_resolver", None)
    return {
        "agent_card": (getattr(agent, "_bundle_id", None), getattr(agent, "_operation", None)),
        "control_card": (getattr(control, "_bundle_id", None), getattr(control, "_operation", None)),
        "credential_release": (github._bundle_id, github._operation, github._secret_ref, getattr(github, "_config_error", "")),
        "membership": (
            getattr(resolver, "_bundle_id", None), getattr(resolver, "_operation", None),
            tuple(sorted(getattr(membership, "_administrative_roles", ()))),
        ),
    }


def test_named_authorities_bind_every_port_exactly_like_the_legacy_block() -> None:
    legacy = _bindings(LEGACY)
    assert legacy["agent_card"] == (BOARD, "project_agent_card_authorize")
    assert legacy["membership"] == (BOARD, "project_membership_resolve", ("admin", "owner"))
    assert _bindings(NAMED) == legacy


def test_an_authorities_only_descriptor_configures_the_ports() -> None:
    """Red before W374: the authorities block was ignored, so every port was unconfigured."""

    module = _module()
    entrypoint = _entrypoint(NAMED)
    assert type(module.descriptor_agent_card_port(entrypoint)).__name__ == "BundleOperationAgentCardAuthorizer"
    assert type(module.descriptor_control_card_port(entrypoint)).__name__ == "BundleOperationControlCardAuthorizer"
    assert module.project_membership_config(entrypoint).provider_configured is True
    calls = []

    async def caller(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "decision": {"allowed": True, "reason": "", "owner_subject": "platform-user-2"}}

    github = module.descriptor_github_authorizer(entrypoint, resolve_secret=_secret, caller=caller)
    answer = asyncio.run(github(access_id="agent-card-1", grantor_subject="platform-user-2",
                                project_ref="work:project:x", repository="example-org/repo"))
    assert answer["decision"]["allowed"] is True
    assert (calls[0]["bundle_id"], calls[0]["operation"]) == (BOARD, "project_agent_github_authorize")


def test_an_empty_authorities_block_reads_the_legacy_provider() -> None:
    assert _bindings({**LEGACY, "authorities": {}}) == _bindings(LEGACY)


def test_named_authorities_never_mix_with_the_legacy_block() -> None:
    """With an authorities block, a name it omits is unconfigured even if the legacy block has it."""

    only_membership = {**LEGACY, "authorities": {"membership": NAMED["authorities"]["membership"]}}
    module = _module()
    entrypoint = _entrypoint(only_membership)
    refused = module.descriptor_agent_card_port(entrypoint)
    assert getattr(refused, "_bundle_id", None) is None
    assert module.project_membership_config(entrypoint).provider_configured is True


def test_an_external_authority_kind_is_refused_by_name_until_it_is_implemented() -> None:
    https = {"authorities": {name: {"kind": "https", "bundle_id": BOARD, "operation": "x"}
                             for name in ("membership", "agent_card", "control_card", "credential_release")}}
    module = _module()
    entrypoint = _entrypoint(https)
    with pytest.raises(Exception) as membership:
        module.project_membership_config(entrypoint)
    assert getattr(membership.value, "reason", "") == "authority_kind_unsupported"
    github = module.descriptor_github_authorizer(entrypoint, resolve_secret=_secret)
    answer = asyncio.run(github(access_id="a", grantor_subject="s", project_ref="p", repository="r"))
    assert answer["error"] == "authority_kind_unsupported"
    for port in (module.descriptor_agent_card_port(entrypoint), module.descriptor_control_card_port(entrypoint)):
        assert getattr(port, "_bundle_id", None) is None


@pytest.mark.parametrize("block, reason", [
    ({"members": {"bundle_id": BOARD}}, "authority_name_unknown"),
    ({"membership": {"kind": "ldap"}}, "authority_kind_invalid"),
    ({"membership": {"bundle_id": BOARD, "endpoint": "x"}}, "authority_config_invalid"),
    ({"membership": "problem-board@1-0"}, "authority_config_invalid"),
    ({"membership": {"administrative_roles": 3}}, "authority_administrative_roles_invalid"),
])
def test_invalid_authority_configuration_is_refused_by_name(block, reason) -> None:
    with pytest.raises(AuthorityConfigError) as refused:
        authorities_from_mapping(block)
    assert refused.value.reason == reason


def test_roles_read_the_same_as_the_legacy_block() -> None:
    parsed = authorities_from_mapping({"membership": {"administrative_roles": "Owner, admin  owner"}})
    assert parsed["membership"].administrative_roles == ("admin", "owner")
