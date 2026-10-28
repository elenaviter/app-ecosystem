"""Governed Google Slides execution and signed artifact delivery."""
from __future__ import annotations

from collections.abc import Mapping

from kdcube_ai_app.apps.chat.sdk.integrations.connected_accounts import (
    connected_account_auth_failure, resolve_connected_account_claim,
    run_with_connected_account_retry,
)
from kdcube_ai_app.apps.chat.sdk.integrations.file_delivery import resolve_connected_account_access_token
from kdcube_ai_app.apps.chat.sdk.integrations.google.slides_proxy import execute_google_slides_operation
from kdcube_ai_app.apps.chat.sdk.integrations.slides.named_service import make_slides_named_service_provider
from kdcube_ai_app.apps.chat.sdk.integrations.slides.selectors import parse_slides_ref, SlidesValidationError
from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.connector_app_resolution import resolve_connector_app_id
from kdcube_ai_app.apps.chat.sdk.solutions.named_services_providers import NamedServiceContext, NamedServiceRequest, NamedServiceStreamResult

_SERVICE = None


def bind_slides_service(service):
    global _SERVICE
    _SERVICE = service


class GoogleSlidesService:
    async def execute(self, *, operation, claim, tool_name, payload=None, account_id=""):
        return await run_with_connected_account_retry(globals(), where=f"google_slides.{operation}", run=lambda: self._execute_once(operation=operation, claim=claim, tool_name=tool_name, payload=dict(payload or {}), account_id=account_id))

    async def _execute_once(self, *, operation, claim, tool_name, payload, account_id):
        credential = None
        claims = [claim] if isinstance(claim, str) else list(claim)
        if not claims:
            raise ValueError("At least one Google Slides claim is required.")
        selected = str(account_id or "")
        for required in claims:
            credential = await resolve_connected_account_claim(globals(), provider_id="google", connector_app_id=resolve_connector_app_id("google"), claim=required, account_id=selected, tool_name=tool_name)
            if not credential.ok:
                return credential.error_envelope(where=f"google_slides.{operation}")
            selected = credential.account_id or selected
        result = await self._execute_with_access_token(operation=operation, access_token=credential.access_token, payload=payload, account_id=selected)
        ret = result.get("ret") or {}
        if not result.get("ok") and ret.get("category") in {"authorization_failed", "scope_insufficient"} and not ret.get("outcome_unknown") and not ret.get("partial_result"):
            return connected_account_auth_failure(credential, result["error"]["message"])
        return result

    async def _execute_with_access_token(self, *, operation, access_token, payload, account_id):
        result = await execute_google_slides_operation(operation=operation, access_token=access_token, payload=payload)
        if not isinstance(result, Mapping):
            return {"ok": False, "error": {"code": "google_slides_invalid_result", "message": "Google Slides returned an invalid result.", "managed": True}, "ret": {"outcome_unknown": False}}
        result = dict(result)
        if result.get("ok"):
            result["ret"] = {**dict(result.get("ret") or {}), "account_id": account_id}
        return result


async def fetch_google_slides_file(entrypoint, *, user_id, tenant, project, object_ref, bundle_id):
    """Re-resolve the token-bound user's read claim; the URL carries no OAuth token."""
    try:
        parsed = parse_slides_ref(object_ref)
    except SlidesValidationError as exc:
        return {"ok": False, "status": 400, "error": {"code": exc.code, "message": str(exc)}}
    token, failure = await resolve_connected_account_access_token(entrypoint, user_id=user_id, tenant=tenant, project=project, provider_id="google", connector_app_id=resolve_connector_app_id("google"), claim="slides:read", account_id=parsed["account_id"])
    if failure is not None:
        return dict(failure)
    service = GoogleSlidesService()

    async def execute(**kwargs):
        return await service._execute_with_access_token(operation=kwargs["operation"], access_token=token, payload=kwargs.get("payload") or {}, account_id=parsed["account_id"])

    provider = make_slides_named_service_provider(execute_operation=execute, bundle_id=bundle_id)
    response = await provider.dispatch(NamedServiceContext(tenant=tenant, project=project, user_id=user_id), NamedServiceRequest(operation="object.get", namespace="slides", object_ref=object_ref, response_mode="stream"))
    if not isinstance(response, NamedServiceStreamResult):
        return {"ok": False, "status": response.status or 502, "error": response.error.to_dict() if response.error else {"code": "slides_file_unavailable", "message": "The presentation file could not be produced."}}
    return {"ok": True, "chunks": response.chunks, "filename": response.filename, "mime_type": response.media_type, "headers": dict(response.headers or {}), "status": response.status_code or 200}
