"""W652: a delivered session's issuance-time facts never outlive the live Card at the Hub readers.

The real enforcing path is the SDK guard (``surface_guard._authorize_delegated_managed_request``):
it loads the STORED access-grant record, re-derives it from the composed LIVE Card
(``_live_grant_record``; a revoked or absent Card is refused), builds the envelope from that live
record, and asks ``authorize_credential_boundary`` -> ``DelegatedCredentialView.from_parts``.
Every stored access grant also keeps the ISSUANCE ``resource_grants`` at its top level
(``oauth/store.py``), which the live re-derivation does not rewrite. ``from_parts`` must therefore
take the resource map whole from the first source that carries one (the live credential), never
re-add a resource only the stale issuance map names.

The other reader, ``oauth/grants.py``, derives the session's permissions from its issuance scopes;
``surface_policy`` only ever requires them IN ADDITION (``issuperset``) to the live grants, so a
stale permission alone cannot grant a capability. Both are pinned here through the actual guard.
"""

from __future__ import annotations

import json

import pytest

from connection_hub.delegated_credentials.credential_view import DelegatedCredentialView

H = pytest.importorskip(
    "kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.oauth.tests.test_surface_guard",
    reason="the SDK surface-guard harness drives the actual enforcing path")

SECOND = "http://testserver/second"
CONNECTIONS = {"delegated_credentials": {"oauth": {"enabled": True, "resources": [
    {"resource": H.GUARD_RESOURCE, "grants": ["records:read"], "tools": {"records_export": {"grants": ["records:read"]}}},
    {"resource": SECOND, "grants": ["records:read"], "tools": {"records_export": {"grants": ["records:read"]}}}]}}}


@pytest.fixture(autouse=True)
def _projections_swept(monkeypatch):
    """The harness module's own autouse fixture: this run's Card projections are swept."""
    async def _ready(self):
        return True

    async def _read_in_current_run(self, access_id):
        return True, await self.read(access_id)

    monkeypatch.setattr(H.CardProjectionEpochGate, "is_ready", _ready)
    monkeypatch.setattr(H._CardCache, "read_in_current_run", _read_in_current_run)


def _issued(**changes):
    """A stored access grant as issued: the pointer plus the issuance maps at its top level."""
    record = H._pointer_grant()
    record["resource_grants"] = {H.GUARD_RESOURCE: ["records:read"], SECOND: ["records:read"]}
    record["resource_operations"] = {H.GUARD_RESOURCE: ["records_export"], SECOND: ["records_export"]}
    record.update(changes)
    return record


def _live(resource_grants, resource_operations):
    return H._live_card(resource_grants=resource_grants, resource_operations=resource_operations)


def _call(monkeypatch, live, path, *, stored=None, user=None):
    redis = H._Redis()
    if live is not None:
        H._store_live_card(redis, live)
    client = H._client(monkeypatch, grant_record=stored if stored is not None else _issued(), redis=redis,
                       connections=CONNECTIONS, user=user)
    response = client.post(path, json=H._rpc_tool_call(), headers={"Authorization": "Bearer reader"})
    return response.status_code, response.json()


def _allowed(result):
    return result == (200, {"ok": True})


def _tool_error(result):
    status, body = result
    assert status == 200 and body["result"]["isError"] is True, result
    return json.loads(body["result"]["content"][0]["text"])["error"]["code"]


# ── reader 1: DelegatedCredentialView.from_parts over the live-resolved record ──────────────────


def test_a_resource_the_live_card_dropped_is_refused_although_the_issued_record_still_names_it(monkeypatch):
    """The discriminating case: the live Card grants only R1 (an operation selection on R2 remains);
    the session's stored record still carries R2 from issuance. R2 must be refused, exactly as for a
    record that never named it."""
    live = _live({H.GUARD_RESOURCE: ("records:read",)},
                 {H.GUARD_RESOURCE: ("records_export",), SECOND: ("records_export",)})
    never_named = _call(monkeypatch, live, "/second",
                        stored=_issued(resource_grants={H.GUARD_RESOURCE: ["records:read"]}))
    issued = _call(monkeypatch, live, "/second")
    assert never_named[0] == 403 and never_named[1]["error_description"] == "delegated credential resource mismatch"
    assert issued == never_named  # the stale issuance entry adds nothing
    assert _allowed(_call(monkeypatch, live, "/guard"))  # the resource the live Card keeps still works


def test_a_resource_removed_with_its_operations_is_refused(monkeypatch):
    """Before W652's reader fix the live outer-operation guard refused this one downstream; now the
    resource boundary refuses it first, exactly as for a record that never named the resource."""
    live = _live({H.GUARD_RESOURCE: ("records:read",)}, {H.GUARD_RESOURCE: ("records_export",)})
    status, body = _call(monkeypatch, live, "/second")
    assert status == 403 and body["error_description"] == "delegated credential resource mismatch"


def test_the_live_outer_operation_guard_refuses_an_operation_the_live_card_dropped(monkeypatch):
    """The downstream live guard (acceptance 3): the resource and its grant are still on the live Card,
    its outer operation is not, while the stored record still names it."""
    live = _live({H.GUARD_RESOURCE: ("records:read",), SECOND: ("records:read",)},
                 {H.GUARD_RESOURCE: ("records_export",)})
    status, body = _call(monkeypatch, live, "/second")
    assert status == 200 and body["result"]["isError"] is True, (status, body)
    error = json.loads(body["result"]["content"][0]["text"])
    assert error["error"]["code"] == "delegated_capability_not_granted"
    assert error["ret"]["requested_capability"]["kind"] == "outer_operation"


def test_a_claim_narrowed_inside_a_kept_resource_is_refused(monkeypatch):
    live = _live({H.GUARD_RESOURCE: (), SECOND: ("records:read",)},
                 {H.GUARD_RESOURCE: ("records_export",), SECOND: ("records_export",)})
    assert _tool_error(_call(monkeypatch, live, "/guard")) == "delegated_capability_not_granted"
    assert _allowed(_call(monkeypatch, live, "/second"))


def test_an_unchanged_live_card_still_allows_every_issued_resource(monkeypatch):
    live = _live({H.GUARD_RESOURCE: ("records:read",), SECOND: ("records:read",)},
                 {H.GUARD_RESOURCE: ("records_export",), SECOND: ("records_export",)})
    assert _allowed(_call(monkeypatch, live, "/guard")) and _allowed(_call(monkeypatch, live, "/second"))


def test_a_revoked_or_unavailable_live_card_fails_closed_whatever_the_session_holds(monkeypatch):
    status, body = _call(monkeypatch, None, "/guard")
    assert status == 503 and body["reason"].startswith("live_grant_")


# ── reader 2: issuance-derived session permissions (oauth/grants.py) ─────────────────────────────


def test_issuance_permissions_never_grant_a_capability_the_live_card_lacks(monkeypatch):
    """The session's permissions only ever ADD requirements; with every permission still held, a live
    narrowing refuses."""
    user = {"sub": "integration:claude:admin", "roles": ["kdcube:role:delegated-client"],
            "permissions": ["kdcube:*:records:*;read", "kdcube:*:*:*;*"]}
    live = _live({H.GUARD_RESOURCE: (), SECOND: ("records:read",)},
                 {H.GUARD_RESOURCE: ("records_export",), SECOND: ("records_export",)})
    assert _tool_error(_call(monkeypatch, live, "/guard", user=user)) == "delegated_capability_not_granted"


# ── the reader's own rule, at the exact record shape the live guard hands it ─────────────────────


def _live_resolved_record():
    """What ``_live_grant_record`` returns: live credential attrs, the stale issuance map on top."""
    return {"registry_access_id": "oauth-access-1", "client_id": "claude",
            "resource_grants": {"https://a.example/mcp": ["read"], "https://b.example/mcp": ["read"]},
            "credential": {"subject": "agent", "attrs": {"client_id": "claude",
                           "resource_grants": {"https://a.example/mcp": ["read"]}}}}


def test_from_parts_takes_the_resource_map_whole_from_the_first_source_that_carries_one():
    record = _live_resolved_record()
    view = DelegatedCredentialView.from_parts(record["credential"], record)
    assert view.resource_grants == {"https://a.example/mcp": ("read",)}


def test_a_legacy_snapshot_keeps_its_issued_map():
    """No live pointer: credential and record hold the same issuance map, or only the record does."""
    both = {"resource_grants": {"https://a.example/mcp": ["read"]},
            "credential": {"attrs": {"resource_grants": {"https://a.example/mcp": ["read"]}}}}
    record_only = {"resource_grants": {"https://a.example/mcp": ["read"]}, "credential": {"attrs": {}}}
    for record in (both, record_only):
        view = DelegatedCredentialView.from_parts(record["credential"], record)
        assert view.resource_grants == {"https://a.example/mcp": ("read",)}


def test_a_present_but_empty_live_map_grants_no_resource_and_never_falls_through(monkeypatch):
    """Main's edge (2026-10-09 02:10Z): a live Card granting no resource is authoritative; neither the
    issuance top-level map nor a single issued ``resource`` brings one back. Fall-through only when the
    map is ABSENT (legacy)."""
    live = _live({}, {})
    for path in ("/guard", "/second"):
        status, body = _call(monkeypatch, live, path)
        assert status == 403 and body["error_description"] in (
            "delegated credential resource is missing", "delegated credential resource mismatch"), (path, status, body)
    record = {"resource": "https://a.example/mcp", "scopes": ["read"],
              "resource_grants": {"https://a.example/mcp": ["read"]},
              "credential": {"attrs": {"resource_grants": {}, "resource": "https://a.example/mcp"}}}
    view = DelegatedCredentialView.from_parts(record["credential"], record)
    assert view.resource_grants == {} and not view.resources


def test_a_legacy_record_without_any_map_still_synthesizes_its_single_resource():
    record = {"resource": "https://a.example/mcp", "scopes": ["read"], "credential": {"attrs": {}}}
    view = DelegatedCredentialView.from_parts(record["credential"], record)
    assert view.resource_grants == {"https://a.example/mcp": ("read",)}
