"""Typed Slides entry points over the shared governed execution pipeline."""
from kdcube_ai_app.apps.chat.sdk.integrations.file_staging import staging_root
from kdcube_ai_app.apps.chat.sdk.integrations.slides.named_service import make_slides_named_service_provider
from kdcube_ai_app.apps.chat.sdk.integrations.slides.selectors import SlidesValidationError, parse_slides_ref
from kdcube_ai_app.apps.chat.sdk.runtime.comm_ctx import get_current_user_identity
from kdcube_ai_app.apps.chat.sdk.solutions.named_services_providers import NamedServiceContext, NamedServiceRequest


def bound_account(operation, presentation_ref, payload, account_id):
    """Resolve ref identity before a tool asks consent for a selected account."""
    selected = account_id
    for ref in (presentation_ref, payload.get("source"), payload.get("source_file")):
        if str(ref or "").startswith("slides:"):
            parsed = parse_slides_ref(ref)
            if selected and selected != parsed["account_id"]:
                raise SlidesValidationError("slides_account_mismatch", "Use the account bound to the presentation ref.")
            selected = parsed["account_id"]
            if operation in ("get_structure", "batch_update") and parsed["kind"] != "presentation":
                raise SlidesValidationError("slides_presentation_ref_required", "Read or batch-update raw structure with a presentation ref, ID or link.")
    if operation in ("create", "import", "search") and presentation_ref:
        raise SlidesValidationError("slides_target_forbidden", "This tool creates or searches presentations. Omit presentation_ref.")
    return selected


async def execute_typed(service, *, operation, tool_name, presentation_ref, payload, account_id):
    account_id = bound_account(operation, presentation_ref, payload, account_id)
    if operation in ("get_structure", "batch_update"):
        claims = ["slides:read"] if operation == "get_structure" else ["slides:read", "slides:write"]
        return await service.execute(operation=operation, claim=claims, tool_name=tool_name, payload={**payload, "presentation_ref": presentation_ref}, account_id=account_id)

    async def execute(**kwargs):
        # Reuse the SDK action pipeline without granting the named-services
        # door: consent and credential resolution name the actual typed tool.
        kwargs["tool_name"] = tool_name
        return await service.execute(**kwargs)

    host = service.host
    provider = make_slides_named_service_provider(
        execute_operation=execute,
        bundle_id=host._named_services_bundle_id() if host else None,
        file_url_factory=host._integration_file_url if host else None,
        staging_root_factory=(lambda: staging_root(str(getattr(host.settings, "STORAGE_PATH", "") or ""))) if host else None,
        provider_fetch_url_factory=host._provider_fetch_url if host else None,
    )
    identity = get_current_user_identity()
    ctx = NamedServiceContext(
        tenant=str(identity.get("tenant_id") or ""), project=str(identity.get("project_id") or ""),
        user_id=identity.get("user_id"), user_type=str(identity.get("user_type") or ""),
        roles=tuple(identity.get("roles") or ()), permissions=tuple(identity.get("permissions") or ()),
        bundle_id=identity.get("bundle_id"), conversation_id=identity.get("conversation_id"),
        session_id=identity.get("session_id"), turn_id=identity.get("turn_id"),
    )
    body = {**payload, "account_id": account_id}
    if operation == "search":
        request = NamedServiceRequest(operation="object.search", namespace="slides", query=payload.get("query", ""), limit=payload.get("limit", 20), cursor=payload.get("cursor", ""), filters={"account_id": account_id, **{key: payload[key] for key in ("url", "presentation_id") if key in payload}})
    elif operation == "get":
        request = NamedServiceRequest(operation="object.get", namespace="slides", object_ref=presentation_ref, filters=body)
    elif operation in ("create", "rename"):
        request = NamedServiceRequest(operation="object.upsert", namespace="slides", object_ref=presentation_ref, payload=body)
    else:
        request = NamedServiceRequest(operation="object.action", namespace="slides", action=operation, object_ref=presentation_ref, payload=body)
    result = await provider.dispatch(ctx, request)
    return result.to_dict()
