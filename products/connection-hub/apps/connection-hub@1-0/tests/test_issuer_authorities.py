"""Thin app composition using descriptor strings and independent workload proof."""

import importlib.util
from collections.abc import Mapping
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from connection_hub.delegated_credentials.issuer_gate import IssuerRequest, IssuerWriteRefused, change_digest
from connection_hub.delegated_credentials.remote_issuer import verify_issuer_request, verify_issuer_envelope


def module():
    path = Path(__file__).resolve().parents[1] / "services" / "issuer_authorities.py"
    spec = importlib.util.spec_from_file_location("issuer_authorities_under_test", path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def config(kind="opaque", **changes):
    return {"delegated_credentials": {"issuer_authorities": {kind: {
        "bundle_id": "authority@1-0", "operation": "opaque_decide",
        "prepare_operation": "opaque_prepare", "finalize_operation": "opaque_finalize",
        "service_id": "configured-hub", "peer_proof_secret_ref": "existing.approved.secret",
        "adapter_id": "authority:v1", **changes,
    }}}}


def request(kind="opaque"):
    return IssuerRequest("authenticated-actor", "request", "update", "card", 3,
                         kind, "opaque-record", change_digest({"label": "new"}), "")


@pytest.mark.asyncio
async def test_configured_ports_use_independent_signed_full_payload_and_fresh_nonces():
    calls, nonces = [], set()
    secret = "x" * 32
    resolve_secret = AsyncMock(return_value=secret)

    async def caller(**kwargs):
        calls.append(kwargs)
        assert kwargs["bundle_id"] == "authority@1-0"
        assert kwargs["route"] == "public" and kwargs["http_method"] == "POST"
        body = kwargs["data"]
        nonce = body["service_proof"]["nonce"]
        assert nonce not in nonces
        nonces.add(nonce)
        verification = dict(secret=secret, bundle_id=kwargs["bundle_id"], operation=kwargs["operation"],
                            expected_service_id="configured-hub", body=body)
        if kwargs["operation"] == "opaque_decide":
            assert verify_issuer_request(**verification).allowed
            return {"ok": True, "request": body["request"], "decision": {
                "allowed": True, "reason": "", "policy_version": "v1",
                "valid_until": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
            }}
        prepare = kwargs["operation"] == "opaque_prepare"
        assert verify_issuer_envelope(**verification, protocol="issuer-context.v1" if prepare else "issuer-outcome.v1").allowed
        if prepare:
            assert body["current"] == {"access_id": "card", "card_revision": 3}
            assert body["candidate"] == {"label": "new"}
            assert secret not in repr(body)
            return {"ok": True, "request": body["request"], "change_digest": change_digest(body["candidate"]),
                    "context_ref": "server-context"}
        return {"ok": True, "request": body["request"], "outcome": body["outcome"], "finalized": True}

    registry = module().issuer_registry_from_connections(connections=config(), resolve_secret=resolve_secret, caller=caller)
    prepared = await registry.prepare(request(), current={"access_id": "card", "card_revision": 3}, candidate={"label": "new"})
    issued = await registry.decide(prepared)
    assert issued.allowed
    assert (await registry.revalidate(prepared, issued)).allowed
    assert await registry.finalize(prepared, state="committed", card_revision=4)
    assert [call["operation"] for call in calls] == ["opaque_prepare", "opaque_decide", "opaque_decide", "opaque_finalize"]
    assert len(nonces) == 4
    assert all(call.args == ("existing.approved.secret",) for call in resolve_secret.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("secret", ["", "short", None, RuntimeError("secret provider private details")])
async def test_missing_secret_named_refusal_before_any_peer_call(secret):
    resolver = AsyncMock(**({"side_effect": secret} if isinstance(secret, Exception) else {"return_value": secret}))
    caller = AsyncMock()
    registry = module().issuer_registry_from_connections(connections=config(), resolve_secret=resolver, caller=caller)
    with pytest.raises(IssuerWriteRefused, match="issuer_peer_proof_not_configured"):
        await registry.prepare(request(), current={}, candidate={"label": "new"})
    caller.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [None, [], {"application": {"operation": False}}, {" application ": {}}])
async def test_declared_malformed_configuration_cannot_restore_owner_bypass(raw):
    settings = {"delegated_credentials": {"issuer_authorities": raw}}
    registry = module().issuer_registry_from_connections(connections=settings, resolve_secret=AsyncMock(), caller=AsyncMock())
    if raw is None:  # absent configuration preserves ordinary local owner rules
        assert not registry.is_managed("application")
        return
    assert registry.is_managed("application")
    assert not (await registry.decide(replace(request("application"), context_ref="server"))).allowed


@pytest.mark.asyncio
async def test_unknown_required_issuer_and_optional_prepare_port_fail_closed():
    caller = AsyncMock()
    resolver = AsyncMock(return_value="x" * 32)
    registry = module().issuer_registry_from_connections(connections={}, resolve_secret=resolver, caller=caller)
    assert registry.is_managed("opaque")
    assert not (await registry.decide(replace(request(), context_ref="server"))).allowed
    registry = module().issuer_registry_from_connections(connections=config(prepare_operation=""), resolve_secret=resolver, caller=caller)
    with pytest.raises(IssuerWriteRefused, match="issuer_context_provider_unavailable"):
        await registry.prepare(request(), current={}, candidate={"label": "new"})
    caller.assert_not_called()


def test_app_factory_binds_registry_and_real_session_actor_not_browser_proofs():
    # Complement mounted entrypoint tests without requiring a live platform SDK.
    import ast
    tree = ast.parse((Path(__file__).resolve().parents[1] / "entrypoint.py").read_text())
    factory = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "_automation_access_service_for")
    binding = next(node for node in ast.walk(factory) if isinstance(node, ast.Call)
                   and isinstance(node.func, ast.Attribute) and node.func.attr == "bind_issuer_registry")
    assert ast.unparse(binding.args[0]) == "issuers"
    keywords = {keyword.arg: keyword.value for keyword in binding.keywords}
    assert ast.unparse(keywords["actor_subject"]) == "_platform_user_id(entrypoint)"
    forbidden = {"_issuer_decision", "_issuer_request_id", "_issuer_context_ref", "service_proof", "change_digest"}
    for name in ("delegated_access_update", "delegated_access_revoke", "control_card_update"):
        handler = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == name)
        calls = [node for node in ast.walk(handler) if isinstance(node, ast.Call)]
        assert not any(keyword.arg in forbidden for call in calls for keyword in call.keywords)
        for call in calls:
            if not isinstance(call.func, ast.Attribute) or call.func.attr not in (
                    "update_access", "revoke_access", "control_card_update"):
                continue
            for keyword in call.keywords:
                if keyword.arg is None:
                    # The existing Control editor expands a server allowlist,
                    # not the original browser payload or arbitrary kwargs.
                    assert ast.unparse(keyword.value) == "_control_card_changes(payload)"
    helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                  and node.name == "_control_card_changes")
    namespace = {"Mapping": Mapping, "Any": object, "Dict": dict,
                 "_expected_card_revision": lambda payload: 3}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), "control-edit-allowlist", "exec"), namespace)
    changes = namespace["_control_card_changes"]({
        **{key: "caller-supplied" for key in forbidden}, "label": "ordinary edit",
    })
    assert changes["label"] == "ordinary edit"
    assert forbidden.isdisjoint(changes)
