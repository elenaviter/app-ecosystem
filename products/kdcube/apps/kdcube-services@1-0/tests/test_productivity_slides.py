"""Typed discovery, default-closed enforcement and shared Slides semantics."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import load_dynamic_module_for_path

BUNDLE_ROOT = Path(__file__).resolve().parents[1]


def module(path="surfaces/mcp/productivity_slides.py"):
    return load_dynamic_module_for_path(BUNDLE_ROOT / path)[1]


class Tools:
    def __init__(self):
        self.tools = {}
    def tool(self, **metadata):
        def bind(function):
            self.tools[metadata["name"]] = function
            return function
        return bind


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,payload,claims", [
    ("get", {}, ["slides:read"]),
    ("add_table", {"values": [["Heading"]]}, ["slides:read", "slides:write"]),
    ("add_video", {"video": "https://youtu.be/AbCdEf012_-"}, ["slides:read", "slides:write"]),
    ("create_comment", {"content": "Review"}, ["slides:read", "slides:comment"]),
    ("delete_comment", {"comment_ref": "c"}, ["slides:read", "slides:comment"]),
    ("trash", {}, ["slides:delete"]),
    ("copy", {"title": "Copy", "parent_id": "folder"}, ["slides:read", "slides:write", "drive:write"]),
])
async def test_tools_enforce_exact_claims_on_the_ref_bound_account_before_execution(tool, payload, claims):
    m = module()
    tools, seen = Tools(), []
    async def enforce(name, operation, account, **kwargs):
        seen.append(("gate", name, operation, account, kwargs["requirements"]))
    async def execute(**kwargs):
        seen.append(("execute", kwargs))
        return {"ok": True}
    m.register_google_slides_tools(tools, service=SimpleNamespace(execute_typed=execute), enforce_tool=enforce)
    result = await tools.tools[f"productivity_slides_{tool}"](payload, presentation_ref="slides:google:account:presentation:deck")
    assert result["ok"]
    assert seen[0] == ("gate", f"productivity_slides_{tool}", tool, "account", [{"provider_id": "google", "claims": claims}])
    assert seen[1][1]["account_id"] == "account"
    assert seen[1][1]["tool_name"] == f"productivity_slides_{tool}"


@pytest.mark.asyncio
async def test_a_denial_is_returned_without_resolving_credentials_or_calling_the_service():
    m = module()
    tools = Tools()
    denial = {"ok": False, "error": {"code": "agent_grant_required"}}
    async def enforce(*args, **kwargs):
        return denial
    async def execute(**kwargs):
        pytest.fail("A denied tool must not execute")
    m.register_google_slides_tools(tools, service=SimpleNamespace(execute_typed=execute), enforce_tool=enforce)
    result = await tools.tools["productivity_slides_add_video"]({"video": "https://youtu.be/AbCdEf012_-"}, presentation_ref="deck")
    assert result is denial


@pytest.mark.asyncio
async def test_bound_account_mismatch_refuses_before_the_gate():
    m = module()
    tools = Tools()
    async def gate(*args, **kwargs):
        pytest.fail("A mismatched target must be refused first")
    m.register_google_slides_tools(tools, service=None, enforce_tool=gate)
    result = await tools.tools["productivity_slides_get"]({}, presentation_ref="slides:google:bound:presentation:deck", account_id="other")
    assert result["error"]["code"] == "slides_account_mismatch"


@pytest.mark.asyncio
async def test_raw_tools_keep_the_mandatory_revision_and_read_write_claims():
    m = module()
    tools, seen = Tools(), []
    async def gate(*args, **kwargs):
        seen.append(kwargs["requirements"])
    async def execute(**kwargs):
        seen.append(kwargs)
        return {"ok": True}
    m.register_google_slides_tools(tools, service=SimpleNamespace(execute_typed=execute), enforce_tool=gate)
    await tools.tools["productivity_slides_get_structure"]("deck")
    assert seen[0][0]["claims"] == ["slides:read"]
    await tools.tools["productivity_slides_batch_update"]("deck", [{"deleteObject": {"objectId": "shape"}}], "revision")
    assert seen[2][0]["claims"] == ["slides:read", "slides:write"]
    assert seen[3]["payload"]["requiredRevisionId"] == "revision"


@pytest.mark.asyncio
async def test_real_mcp_discovery_exposes_structured_payloads_and_safe_raw_schema():
    m = module("surfaces/mcp/productivity.py")
    app = m.build_productivity_mcp_app(name="Slides schema test", config_factory=lambda: {"connector_apps": {"google": "gmail"}}, tenant_factory=lambda: "t", project_factory=lambda: "p")
    tools = {tool.name: tool for tool in await app.list_tools() if tool.name.startswith("productivity_slides_")}
    assert len(tools) == 38
    schema = tools["productivity_slides_add_table"].input_schema
    assert schema["properties"]["payload"]["properties"]["values"]
    Draft202012Validator.check_schema(schema)
    compose = tools["productivity_slides_compose"].input_schema
    Draft202012Validator.check_schema(compose)
    Draft202012Validator(compose).validate({"payload": {"cells": [{"at": [0, 0], "element": {"table": {"values": [["Header"]]}}}]}, "presentation_ref": "deck"})
    batch = tools["productivity_slides_batch_update"].input_schema
    assert "requiredRevisionId" in batch["required"]
    assert batch["properties"]["requests"]["maxItems"] == 50
    raw = tools["productivity_slides_get_structure"].input_schema
    assert raw["properties"]["slide_limit"]["default"] == 10
    assert {"slide_selector", "start_slide", "slide_limit", "include"} <= raw["properties"].keys()
    for name in ("search", "get"):
        natural = tools[f"productivity_slides_{name}"].input_schema
        Draft202012Validator(natural).validate({"payload": {"url": "https://docs.google.com/presentation/d/deck/edit"}})
        assert {"url", "presentation_id"} <= natural["properties"]["payload"]["properties"].keys()
    for name in ("style_text", "replace_text", "create_comment"):
        assert tools[f"productivity_slides_{name}"].input_schema["properties"]["payload"]["properties"]["occurrence"]["minimum"] == 1
    assert all(tool.title and tool.description for tool in tools.values())


def test_both_catalogs_match_every_typed_tool_and_its_exact_claims():
    import kdcube_ai_app
    import yaml
    m = module()
    platform = Path(kdcube_ai_app.__file__).resolve().parents[3] / "deployment/bundles.yaml"
    ecosystem = BUNDLE_ROOT.parents[3] / "products/connection-hub/apps/connection-hub@1-0/config/bundles.template.yaml"
    rows = []
    for path in (platform, ecosystem):
        config = yaml.safe_load(path.read_text())["bundles"]["items"][0]["config"]["connections"]["delegated_credentials"]["oauth"]
        resource = next(r for r in config["resources"] if "/mcp/productivity" in r["resource"])
        rows.append({key: value for key, value in resource["tools"].items() if key.startswith("productivity_slides_")})
    assert rows[0] == rows[1]
    assert set(rows[0]) == set(m.SLIDES_PRODUCTIVITY_TOOLS)
    for name, row in rows[0].items():
        claims = m.SLIDES_PRODUCTIVITY_TOOLS[name]["connections"]["delegated_to_kdcube"]["connected_accounts"][0]["claims"]
        assert row["grants"] == claims
        assert row["label"] and row["description"]


@pytest.mark.asyncio
async def test_sdk_action_pipeline_retains_the_typed_tool_name_and_request_identity(monkeypatch):
    m = module("services/productivity/slides_typed.py")
    seen = []
    async def execute(**kwargs):
        seen.append(kwargs)
        return {"ok": True, "ret": {"presentation_id": "deck", "revision_id": "r2", "account_id": "account", "changes": {"action": "reply_comment"}}}
    monkeypatch.setattr(m, "get_current_user_identity", lambda: {"tenant_id": "tenant", "project_id": "project", "user_id": "user", "conversation_id": "conversation"})
    factory = m.make_slides_named_service_provider
    contexts = []
    def observed_factory(**kwargs):
        provider = factory(**kwargs)
        async def dispatch(ctx, request):
            contexts.append(ctx)
            return await provider.dispatch(ctx, request)
        return SimpleNamespace(dispatch=dispatch)
    monkeypatch.setattr(m, "make_slides_named_service_provider", observed_factory)
    service = SimpleNamespace(host=None, execute=execute)
    result = await m.execute_typed(service, operation="reply_comment", tool_name="productivity_slides_reply_comment", presentation_ref="slides:google:account:presentation:deck", payload={"comment_ref": "comment", "content": "Reply"}, account_id="")
    assert result["ok"]
    assert seen[0]["tool_name"] == "productivity_slides_reply_comment"
    assert seen[0]["claim"] == ["slides:read", "slides:comment"]
    assert seen[0]["account_id"] == "account"
    assert seen[0]["payload"]["presentation_ref"] == "deck"
    assert (contexts[0].tenant, contexts[0].project, contexts[0].user_id, contexts[0].conversation_id) == ("tenant", "project", "user", "conversation")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["search", "get"])
async def test_typed_natural_lookup_reaches_the_sdk_and_returns_a_bound_ref(monkeypatch, operation):
    m = module("services/productivity/slides_typed.py")
    seen = []
    async def execute(**kwargs):
        seen.append(kwargs)
        row = {"presentation_id": "deck", "account_id": "chosen", "title": "Deck", "snapshot": {"slides": [], "range": {"next_slide": None}}}
        return {"ok": True, "ret": {**row, "items": [row]}}
    monkeypatch.setattr(m, "get_current_user_identity", lambda: {})
    result = await m.execute_typed(SimpleNamespace(host=None, execute=execute), operation=operation, tool_name=f"productivity_slides_{operation}", presentation_ref="", payload={"url": "https://docs.google.com/presentation/d/deck/edit?slide=id.s1"}, account_id="chosen")
    assert result["ok"], result
    row = result["ret"]["items"][0] if operation == "search" else result["ret"]["object"]
    assert row["ref"] == "slides:google:chosen:presentation:deck"
    assert seen[0]["tool_name"] == f"productivity_slides_{operation}" and seen[0]["account_id"] == "chosen"
    assert seen[0]["payload"].get("presentation_id", seen[0]["payload"].get("presentation_ref")) == "deck"


@pytest.mark.asyncio
async def test_raw_read_options_survive_the_tool_dispatch():
    m = module()
    tools, seen = Tools(), []
    async def gate(*args, **kwargs):
        return None
    async def execute(**kwargs):
        seen.append(kwargs)
        return {"ok": True}
    m.register_google_slides_tools(tools, service=SimpleNamespace(execute_typed=execute), enforce_tool=gate)
    await tools.tools["productivity_slides_get_structure"]("deck", slide_selector={"position": 13}, include=["layouts"])
    assert seen[0]["payload"] == {"slide_selector": {"position": 13}, "start_slide": 1, "slide_limit": 10, "include": ["layouts"]}
