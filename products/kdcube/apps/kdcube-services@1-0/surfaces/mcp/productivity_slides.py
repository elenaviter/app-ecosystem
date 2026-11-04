"""Typed Slides tools, with exact claims and shared SDK action payloads."""
from typing import Annotated, Any, Awaitable, Callable

from jsonschema import Draft202012Validator
from pydantic import Field
from connection_hub.mcp_metadata import destructive_annotations, read_only_annotations, write_annotations
from kdcube_ai_app.apps.chat.sdk.integrations.slides.native import ALLOWED_REQUEST_KINDS
from kdcube_ai_app.apps.chat.sdk.integrations.slides.schema import SLIDES_PRESENTATION, SLIDES_SCHEMA, action_claims
from kdcube_ai_app.apps.chat.sdk.integrations.slides.selectors import SlidesValidationError
from kdcube_ai_app.apps.chat.sdk.integrations.slides.typed_schema import TYPED_OPERATIONS, inline_schema, payload_schema

from ...services.productivity.slides_typed import bound_account

EnforceTool = Callable[..., Awaitable[dict[str, Any] | None]]
_BASIC = {
    "search": ("Find presentations", "Search presentation titles; an empty query lists recent decks."),
    "get": ("Read a presentation", "Read a bounded snapshot and revision before editing."),
    "create": ("Create a presentation", "Create an empty presentation with a title."),
    "rename": ("Rename a presentation", "Change a presentation title."),
    "get_structure": ("Read native presentation structure", "Read masked native JSON with object IDs, EMU geometry and revision_id for a raw batch."),
    "batch_update": ("Batch-edit a presentation", "Apply 1–50 allowlisted native requests with mandatory requiredRevisionId from get_structure. Slide deletion and comments use dedicated tools."),
}


def claims_for(operation, payload=None):
    if operation == "get_structure":
        return ["slides:read"]
    return action_claims(operation, payload)


def requirements(claims):
    return [{"provider_id": "google", "claims": list(claims)}]


def metadata(operation):
    label, description = _BASIC.get(operation, (SLIDES_PRESENTATION.get(operation, {}).get("label", ""), SLIDES_PRESENTATION.get(operation, {}).get("description", "")))
    return {"label": label, "description": description, "connections": {"delegated_to_kdcube": {"connected_accounts": requirements(claims_for(operation))}}}


SLIDES_PRODUCTIVITY_TOOLS = {f"productivity_slides_{op}": metadata(op) for op in (*TYPED_OPERATIONS, "get_structure", "batch_update")}


def refusal(exc):
    return {"ok": False, "error": {"code": exc.code, "message": str(exc), "details": exc.details, "managed": True}, "ret": {"outcome_unknown": False}}


def register_google_slides_tools(mcp: Any, *, service: Any, enforce_tool: EnforceTool):
    from mcp.types import ToolAnnotations

    async def dispatch(operation, payload, presentation_ref, account_id):
        try:
            selected = bound_account(operation, presentation_ref, payload, account_id)
            claims = claims_for(operation, payload)
            denial = await enforce_tool(f"productivity_slides_{operation}", operation, selected, requirements=requirements(claims))
            if denial is not None:
                return denial
            if operation in TYPED_OPERATIONS:
                error = next(Draft202012Validator(payload_schema(operation)).iter_errors(payload), None)
                if error:
                    raise SlidesValidationError("slides_invalid_payload", "Use the fields and types declared by this tool payload schema.", details={"path": list(error.absolute_path)[:10]})
            return await service.execute_typed(operation=operation, tool_name=f"productivity_slides_{operation}", presentation_ref=presentation_ref, payload=payload, account_id=selected)
        except SlidesValidationError as exc:
            return refusal(exc)

    def register_action(operation):
        async def invoke(payload, presentation_ref: Annotated[str, Field(description="Presentation ID, Google Slides link or bound ref. Omit for search/create/import.")] = "", account_id: Annotated[str, Field(description="Connected account ID; a bound presentation ref fixes this account.")] = ""):
            return await dispatch(operation, payload, presentation_ref, account_id)
        invoke.__name__ = f"productivity_slides_{operation}"
        invoke.__annotations__["payload"] = Annotated[dict[str, Any], Field(description="Typed action arguments; read the tool instruction and preview edits.", json_schema_extra=inline_schema(payload_schema(operation)))]
        meta = metadata(operation)
        instruction = SLIDES_SCHEMA["actions"].get(operation, {}).get("description", meta["description"])
        annotation = read_only_annotations if claims_for(operation) == ["slides:read"] else destructive_annotations if operation in ("delete_slide", "delete_comment", "trash") else write_annotations
        mcp.tool(name=invoke.__name__, title=meta["label"], description=f'{meta["description"]} {instruction}', annotations=annotation(ToolAnnotations, title=meta["label"]), structured_output=False)(invoke)

    for operation in TYPED_OPERATIONS:
        register_action(operation)

    @mcp.tool(name="productivity_slides_get_structure", title=_BASIC["get_structure"][0], description=_BASIC["get_structure"][1], annotations=read_only_annotations(ToolAnnotations, title=_BASIC["get_structure"][0]), structured_output=False)
    async def get_structure(presentation_ref: Annotated[str, Field(description="Presentation ID, link or bound presentation ref.")], account_id: str = ""):
        return await dispatch("get_structure", {}, presentation_ref, account_id)

    @mcp.tool(name="productivity_slides_batch_update", title=_BASIC["batch_update"][0], description=_BASIC["batch_update"][1] + " Allowed: " + ", ".join(sorted(ALLOWED_REQUEST_KINDS)), annotations=destructive_annotations(ToolAnnotations, title=_BASIC["batch_update"][0]), structured_output=False)
    async def batch_update(presentation_ref: Annotated[str, Field(description="Presentation ID, link or bound presentation ref.")], requests: Annotated[list[dict[str, Any]], Field(min_length=1, max_length=50, description="Single-kind native requests from the published allowlist; batch at most 1 MB and 100000 text characters.")], requiredRevisionId: Annotated[str, Field(min_length=1, max_length=1024, description="Exact revision_id from a fresh get_structure; mandatory, never an implicit overwrite.")], preview: bool = False, account_id: str = ""):
        return await dispatch("batch_update", {"requests": requests, "requiredRevisionId": requiredRevisionId, "preview": preview}, presentation_ref, account_id)
