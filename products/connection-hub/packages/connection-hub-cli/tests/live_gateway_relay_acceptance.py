"""Exercise the local MCP relay against a live delegated MCP Gateway.

This opt-in integration test creates a disposable Cognito owner, an
authenticated external MCP fixture, and one short-lived delegated card. It
keeps one local relay session open while authority is narrowed, restored, and
revoked. All test-owned server state is removed in ``finally``.

The test intentionally uses an in-memory credential adapter. It proves the
Relay-to-Gateway protocol and live-card behavior, not native keyring custody or
installation into a particular desktop MCP client. The KDCube source checkout
must be present on ``PYTHONPATH`` because its disposable live-test support owns
the local fixture and temporary Cognito user lifecycle.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from connection_hub_cli.mcp_relay import DownstreamToolChanges, McpToolRelay
from connection_hub_cli.profile_connection import connect_profile_tools
from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.remote_mcp.tests.live_acceptance import (
    Fixture,
)
from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.remote_mcp.tests.live_support import (
    DisposableOwner,
    OwnerOperations,
)
from mcp import Client

PROFILE_NAME = "live-gateway-relay"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:5173")
    parser.add_argument("--tenant", default="demo-tenant")
    parser.add_argument("--project", default="demo-project")
    parser.add_argument("--bundle-id", default="connection-hub@1-0")
    parser.add_argument("--cognito-region", default="eu-west-1")
    parser.add_argument("--cognito-pool", required=True)
    parser.add_argument("--cognito-client", required=True)
    parser.add_argument("--fixture-container", default="kdcube-gateway-relay-fixture")
    parser.add_argument("--fixture-image", default="kdcube-chat-proc:latest")
    parser.add_argument("--fixture-port", type=int, default=8767)
    parser.add_argument("--source-root", type=Path, required=True)
    return parser.parse_args()


class _Profiles:
    def __init__(self, *, endpoint: str) -> None:
        self.profile = SimpleNamespace(
            name=PROFILE_NAME,
            endpoint=endpoint,
            credential_ref="live-gateway-relay-credential",
            auth_type="static_bearer",
        )

    def require(self, name: str) -> Any:
        assert name == PROFILE_NAME
        return self.profile


class _Credentials:
    def __init__(self, bearer: str) -> None:
        self._bearer = bearer
        self.reads = 0

    def get(self, credential_ref: str) -> str:
        assert credential_ref == "live-gateway-relay-credential"
        self.reads += 1
        return self._bearer


def _is_error(result: Any) -> bool:
    value = getattr(result, "is_error", None)
    if value is None:
        value = getattr(result, "isError", None)
    return bool(value)


def _structured(result: Any) -> dict[str, Any]:
    value = getattr(result, "structured_content", None)
    if value is None:
        value = getattr(result, "structuredContent", None)
    return dict(value) if isinstance(value, Mapping) else {}


def _gateway_routes(tools: list[Any]) -> dict[tuple[str, str], str]:
    routes: dict[tuple[str, str], str] = {}
    for tool in tools:
        raw_meta = getattr(tool, "meta", None) or getattr(tool, "_meta", None) or {}
        if hasattr(raw_meta, "model_dump"):
            raw_meta = raw_meta.model_dump(mode="json", by_alias=True)
        meta = raw_meta if isinstance(raw_meta, Mapping) else {}
        raw_route = meta.get("connection_hub") or {}
        route = raw_route if isinstance(raw_route, Mapping) else {}
        resource = str(route.get("resource_id") or "").strip()
        operation = str(route.get("operation") or "").strip()
        name = str(getattr(tool, "name", "") or "").strip()
        if resource and operation and name:
            key = (resource, operation)
            assert key not in routes, key
            routes[key] = name
    return routes


def _owner_card(owner: OwnerOperations, *, access_id: str) -> dict[str, Any]:
    listing = owner.call("GET", "delegated_access_list")
    assert listing.get("ok") is True, listing
    matches = [
        dict(item)
        for item in listing.get("items") or []
        if str(item.get("access_id") or "") == access_id
    ]
    assert len(matches) == 1, matches
    return matches[0]


def _set_always(
    owner: OwnerOperations, *, access_id: str, resource: str, operation: str
) -> None:
    result = owner.call(
        "POST",
        "delegated_invocation_policy_set",
        {
            "access_id": access_id,
            "resource": resource,
            "operation": operation,
            "mode": "always",
        },
    )
    assert result.get("ok") is True, result


async def _run(args: argparse.Namespace) -> None:
    upstream_bearer = secrets.token_urlsafe(32)
    fixture = Fixture(args, upstream_bearer)
    identity = DisposableOwner(
        region=args.cognito_region,
        pool_id=args.cognito_pool,
        client_id=args.cognito_client,
        label="Connection Hub live Gateway Relay acceptance",
    )
    owner: OwnerOperations | None = None
    connector: dict[str, Any] | None = None
    access_id = ""

    try:
        fixture.start(1)
        owner = OwnerOperations(
            base_url=args.base_url,
            tenant=args.tenant,
            project=args.project,
            bundle_id=args.bundle_id,
            headers=identity.authenticate(),
        )
        owner.wait_ready()

        created = owner.call(
            "POST",
            "remote_mcp_connector_create",
            {
                "label": "Live Gateway Relay fixture",
                "endpoint": fixture.endpoint,
                "credential_mode": "bearer",
                "credential_value": upstream_bearer,
            },
        )
        assert created.get("ok") is True, created
        connector = dict(created["connector"])
        assert upstream_bearer not in json.dumps(connector, sort_keys=True)
        resource = str(connector["resource"])

        created_card = owner.call(
            "POST",
            "delegated_access_create",
            {
                "label": "Live Gateway Relay caller",
                "resource_grants": {resource: ["external_mcp:use"]},
                "resource_operations": {resource: ["search"]},
                "ttl_seconds": 600,
            },
        )
        assert created_card.get("ok") is True, created_card
        bearer = str(created_card.get("access_token") or "")
        access_id = str(created_card.get("access", {}).get("access_id") or "")
        assert bearer and access_id
        _set_always(
            owner,
            access_id=access_id,
            resource=resource,
            operation="search",
        )

        endpoint = f"{owner.base}/public/mcp/delegated_mcp_gateway"
        credentials = _Credentials(bearer)
        changes = DownstreamToolChanges()
        async with connect_profile_tools(
            profile_name=PROFILE_NAME,
            profiles=_Profiles(endpoint=endpoint),
            credentials=credentials,
        ) as (upstream, _upstream_client):
            relay = McpToolRelay(upstream, changes)
            try:
                async with Client(relay.server, mode="legacy", cache=None) as client:
                    listed = await client.list_tools(cache_mode="reload")
                    routes = _gateway_routes(list(listed.tools))
                    assert set(routes) == {(resource, "search")}, routes
                    search_name = routes[(resource, "search")]
                    called = await client.call_tool(
                        search_name,
                        {"query": "live Gateway Relay acceptance"},
                        meta={"connection_hub/invocation_id": "relay-live-1"},
                    )
                    assert not _is_error(called), _structured(called)
                    rendered = called.model_dump_json(by_alias=True)
                    assert upstream_bearer not in rendered
                    assert bearer not in rendered
                    assert (
                        _structured(called)
                        .get("structured_content", {})
                        .get("upstream_credential_verified")
                        is True
                    ), _structured(called)
                    print("PASS one local Relay lists and invokes the live Gateway")

                    current = _owner_card(owner, access_id=access_id)
                    narrowed = owner.call(
                        "POST",
                        "delegated_access_update",
                        {
                            "access_id": access_id,
                            "resource_grants": {resource: ["external_mcp:use"]},
                            "resource_operations": {resource: []},
                            "expected_card_revision": current["card_revision"],
                        },
                    )
                    assert narrowed.get("ok") is True, narrowed
                    relisted = await client.list_tools(cache_mode="reload")
                    assert not _gateway_routes(list(relisted.tools))
                    denied = await client.call_tool(
                        search_name,
                        {"query": "must not dispatch after narrowing"},
                        meta={"connection_hub/invocation_id": "relay-live-2"},
                    )
                    assert _is_error(denied), _structured(denied)
                    print("PASS the running Relay observes live Card narrowing")

                    narrowed_card = _owner_card(owner, access_id=access_id)
                    restored = owner.call(
                        "POST",
                        "delegated_access_update",
                        {
                            "access_id": access_id,
                            "resource_grants": {resource: ["external_mcp:use"]},
                            "resource_operations": {resource: ["search"]},
                            "expected_card_revision": narrowed_card["card_revision"],
                        },
                    )
                    assert restored.get("ok") is True, restored
                    _set_always(
                        owner,
                        access_id=access_id,
                        resource=resource,
                        operation="search",
                    )
                    restored_list = await client.list_tools(cache_mode="reload")
                    restored_routes = _gateway_routes(list(restored_list.tools))
                    assert set(restored_routes) == {(resource, "search")}
                    restored_call = await client.call_tool(
                        restored_routes[(resource, "search")],
                        {"query": "same Relay after regrant"},
                        meta={"connection_hub/invocation_id": "relay-live-3"},
                    )
                    assert not _is_error(restored_call), _structured(restored_call)
                    print("PASS the running Relay observes a focused regrant")

                    revoked = owner.call(
                        "POST", "delegated_access_revoke", {"access_id": access_id}
                    )
                    assert revoked.get("ok") is True, revoked
                    access_id = ""
                    rejected = await client.call_tool(
                        search_name,
                        {"query": "must not dispatch after revocation"},
                        meta={"connection_hub/invocation_id": "relay-live-4"},
                    )
                    assert _is_error(rejected), _structured(rejected)
                    rejected_json = rejected.model_dump_json(by_alias=True)
                    assert bearer not in rejected_json
                    assert upstream_bearer not in rejected_json
                    print("PASS Card revocation closes the running Relay")
            finally:
                changes.close()

        assert credentials.reads == 1
        print("PASS delegated bearer resolved once and never entered client config")
    finally:
        if owner is not None:
            if access_id:
                try:
                    owner.call(
                        "POST", "delegated_access_revoke", {"access_id": access_id}
                    )
                except Exception:  # noqa: BLE001 - cleanup must continue
                    print("WARN disposable Relay card cleanup was incomplete")
            if connector is not None:
                try:
                    owner.call(
                        "POST",
                        "remote_mcp_connector_delete",
                        {
                            "connector_id": connector.get("connector_id"),
                            "expected_revision": connector.get("revision"),
                        },
                    )
                except Exception:  # noqa: BLE001 - cleanup must continue
                    print("WARN disposable Relay connector cleanup was incomplete")
            owner.close()
        identity.delete()
        fixture.stop()
        print("PASS disposable Relay owner, card, connector, and fixture cleanup")


def main() -> None:
    asyncio.run(_run(_arguments()))


if __name__ == "__main__":
    main()
