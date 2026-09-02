# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Chat-side post-processing for KDCube MCP tool results — shared by every
consumer.

An agent that consumes a KDCube-served MCP surface runs INSIDE a chat turn, so
two things a raw tool result cannot do on its own must happen on the caller
side: a consent denial has to become a chat banner, and a file has to reach the
user as a card (never a signed URL the model re-types). This module is that
post-processor, applied ONCE by the SDK MCP loader, so no bundle re-implements
it and there is nothing to drift.

It is driven ENTIRELY by the result's self-describing consent block — the same
contract every KDCube MCP surface returns:

  * ``delegated_consent_required`` — the AGENT's own grant is missing; the block
    carries ``agent_client_id``, ``resource``, ``claims``, ``namespace`` and (for
    a hosted agent) the one-click ``grant`` action.
  * ``needs_connected_account_consent`` — the user's PROVIDER account is
    missing/expired; the block carries ``provider_id``, ``connector_app_id``,
    ``claims``, ``namespace``, ``url``.

Both banners are raised through the SAME shared announce the NATIVE named-service
path uses, so the two surfaces cannot diverge. An external client (Claude Code)
has no chat lane, so the announce is a no-op and the result — which already
carries the Connection Hub link — flows to it unchanged.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Dict, List, Mapping

from connection_hub.mcp_consent import (
    MCPConsentRequired,
    mcp_consent_from_denial,
)

logger = logging.getLogger(__name__)

DELEGATED_CONSENT_REQUIRED = "delegated_consent_required"
DELEGATED_CAPABILITY_NOT_GRANTED = "delegated_capability_not_granted"
NEEDS_CONNECTED_ACCOUNT_CONSENT = "needs_connected_account_consent"
DELEGATED_INVOCATION_ID_REQUIRED = "delegated_invocation_id_required"
DELEGATED_INVOCATION_LIMIT_EXHAUSTED = "delegated_invocation_limit_exhausted"
REMOTE_MCP_CONNECTOR_NOT_CONSENTED = "connector_grant_not_consented"
REMOTE_MCP_OPERATION_NOT_CONSENTED = "operation_not_consented"
_INVOCATION_POLICY_CODES = (
    DELEGATED_INVOCATION_ID_REQUIRED,
    DELEGATED_INVOCATION_LIMIT_EXHAUSTED,
)
_REMOTE_MCP_GRANT_CODES = (
    REMOTE_MCP_CONNECTOR_NOT_CONSENTED,
    REMOTE_MCP_OPERATION_NOT_CONSENTED,
)
_MARKERS = (
    '"download"',
    f'"{DELEGATED_CONSENT_REQUIRED}"',
    f'"{DELEGATED_CAPABILITY_NOT_GRANTED}"',
    f'"{NEEDS_CONNECTED_ACCOUNT_CONSENT}"',
    f'"{DELEGATED_INVOCATION_ID_REQUIRED}"',
    f'"{DELEGATED_INVOCATION_LIMIT_EXHAUSTED}"',
    f'"{REMOTE_MCP_CONNECTOR_NOT_CONSENTED}"',
    f'"{REMOTE_MCP_OPERATION_NOT_CONSENTED}"',
    '"consent"',
)

ConnectedConsentAnnouncer = Callable[..., Awaitable[Any]]
AgentConsentAnnouncer = Callable[[MCPConsentRequired], Awaitable[None]]
ResultFileDeliverer = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]


def _error_code(parsed: Mapping[str, Any]) -> str:
    err = parsed.get("error")
    if isinstance(err, Mapping):
        return str(err.get("code") or "").strip()
    return str(parsed.get("code") or err or "").strip()


def _consent_block(parsed: Mapping[str, Any]) -> Mapping[str, Any]:
    """The self-describing consent block, wherever the surface placed it —
    top-level ``consent`` or nested under ``error.details.consent``."""
    top = parsed.get("consent")
    if isinstance(top, Mapping):
        return top
    err = parsed.get("error") if isinstance(parsed.get("error"), Mapping) else {}
    details = err.get("details") if isinstance(err.get("details"), Mapping) else {}
    block = details.get("consent")
    return block if isinstance(block, Mapping) else {}


async def announce_result_consent(
    parsed: Dict[str, Any],
    *,
    connected_consent_announcer: ConnectedConsentAnnouncer | None = None,
    agent_consent_announcer: AgentConsentAnnouncer | None = None,
) -> Dict[str, Any] | None:
    """Raise the chat banner a tool result's consent block asks for.

    Returns a model-safe result to substitute (agent-grant: the explainable
    consent result), or the payload itself when it announced but keeps the
    original content (connected-account: the result already carries the link),
    or None when there is no consent to raise. Never raises."""
    try:
        code = _error_code(parsed)
        if code not in (
            DELEGATED_CONSENT_REQUIRED,
            DELEGATED_CAPABILITY_NOT_GRANTED,
            NEEDS_CONNECTED_ACCOUNT_CONSENT,
            *_INVOCATION_POLICY_CODES,
            *_REMOTE_MCP_GRANT_CODES,
        ):
            return None
        block = _consent_block(parsed)
        namespace = str(block.get("namespace") or parsed.get("namespace") or "").strip()
        operation = str(block.get("operation") or parsed.get("operation") or "").strip()
        outer_operation = str(block.get("outer_operation") or "").strip()

        if code in _REMOTE_MCP_GRANT_CODES:
            client_id = str(block.get("agent_client_id") or "").strip()
            resource = str(
                block.get("resource") or parsed.get("resource") or ""
            ).strip()
            if not client_id or not resource or not outer_operation:
                logger.warning(
                    "[mcp-result] remote-MCP grant demand is not announceable: "
                    "client=%r resource=%r operation=%r",
                    client_id,
                    resource,
                    outer_operation,
                )
                return None
            label = str(block.get("tool_name") or outer_operation)
            agent_message = (
                f"{label} is outside this caller profile's current delegated "
                "access. The user can allow this operation once or always in "
                "Connection Hub."
            )
            consent = MCPConsentRequired(
                resource=resource,
                claims=[
                    str(item)
                    for item in block.get("claims") or []
                    if str(item or "").strip()
                ],
                consent=dict(block),
                agent_message=agent_message,
            )
            if agent_consent_announcer is not None:
                await agent_consent_announcer(consent)
            else:
                logger.warning(
                    "[mcp-result] remote-MCP consent announcer is not configured"
                )
            handled = consent.to_tool_result()
            handled["error"]["code"] = code
            return handled

        if code in _INVOCATION_POLICY_CODES:
            client_id = str(block.get("agent_client_id") or "").strip()
            resource = str(block.get("resource") or parsed.get("resource") or "").strip()
            if not client_id or not resource or not outer_operation:
                logger.warning(
                    "[mcp-result] invocation-policy demand is not announceable: "
                    "client=%r resource=%r operation=%r",
                    client_id,
                    resource,
                    outer_operation,
                )
                return None
            label = str(block.get("tool_name") or outer_operation)
            agent_message = (
                f"{label} needs a new invocation allowance on its delegated "
                "access card. It is blocked until the user chooses Once or "
                "Always in Connection Hub."
            )
            consent = MCPConsentRequired(
                resource=resource,
                claims=[],
                consent=dict(block),
                agent_message=agent_message,
            )
            if agent_consent_announcer is not None:
                await agent_consent_announcer(consent)
            else:
                logger.warning(
                    "[mcp-result] invocation-policy consent announcer is not configured"
                )
            handled = consent.to_tool_result()
            handled["error"]["code"] = code
            return handled

        if code == NEEDS_CONNECTED_ACCOUNT_CONSENT:
            if not namespace:
                logger.warning(
                    "[mcp-result] connected-account consent has NO namespace in the block — "
                    "the surface should self-describe it (provider=%s)",
                    block.get("provider_id"),
                )
            logger.info("[mcp-result] connected-account consent -> banner: namespace=%s", namespace)
            if connected_consent_announcer is not None:
                await connected_consent_announcer(
                    parsed,
                    namespace=namespace,
                    tool_name=namespace,
                )
            else:
                logger.warning(
                    "[mcp-result] connected-account consent announcer is not configured"
                )
            # Handled; keep the original result (its link/instructions reach the
            # model and an external client). Return it so callers know it was
            # handled (an empty-return would read as "nothing happened").
            return parsed

        # delegated_consent_required — the agent's own grant. The block is
        # authoritative (the door enriched it from the bearer's credential).
        claims = [str(c) for c in (block.get("claims") or parsed.get("missing_grants") or []) if str(c or "").strip()]
        client_id = str(block.get("agent_client_id") or "").strip()
        resource = str(block.get("resource") or "").strip()
        if not (claims or outer_operation) or not client_id or not resource:
            logger.warning(
                "[mcp-result] agent-grant consent not announceable from block: "
                "client=%r resource=%r claims=%s namespace=%s — the surface must self-describe it",
                client_id, resource, claims, namespace,
            )
            return None
        logger.info(
            "[mcp-result] agent-grant consent -> banner: client=%s resource=%s "
            "claims=%s namespace=%s operation=%s outer_operation=%s",
            client_id, resource, claims, namespace, operation, outer_operation,
        )
        if block.get("invocation_policy") or block.get("invocation_change_id"):
            label = str(block.get("tool_name") or outer_operation or namespace)
            asked = outer_operation or operation or "this operation"
            consent = MCPConsentRequired(
                resource=resource,
                claims=claims,
                consent=dict(block),
                agent_message=(
                    f"{label} needs the user's delegated access for {asked}. "
                    "It is blocked until the user chooses Once or Always in "
                    "Connection Hub."
                ),
            )
            if agent_consent_announcer is not None:
                await agent_consent_announcer(consent)
            else:
                logger.warning("[mcp-result] agent consent announcer is not configured")
            handled = consent.to_tool_result()
            handled["error"]["code"] = code
            return handled
        consent = mcp_consent_from_denial(
            {"status": 403, "reason": "authority_mismatch"},
            resource=resource,
            claims=claims,
            tool_name=str(block.get("tool_name") or namespace),
            agent_client_id=client_id,
            namespace=namespace,
            operation=operation,
            outer_operation=outer_operation,
        )
        if agent_consent_announcer is not None:
            await agent_consent_announcer(consent)
        else:
            logger.warning("[mcp-result] agent consent announcer is not configured")
        return consent.to_tool_result()
    except Exception:  # pragma: no cover - post-processing is best-effort
        logger.info("[mcp-result] consent announce failed (non-fatal)", exc_info=True)
        return None


def _postprocessable(text: str) -> Dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw.startswith("{") or not any(marker in raw for marker in _MARKERS):
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


async def _process_dict(
    parsed: Dict[str, Any],
    *,
    connected_consent_announcer: ConnectedConsentAnnouncer | None,
    agent_consent_announcer: AgentConsentAnnouncer | None,
    file_deliverer: ResultFileDeliverer | None,
) -> Dict[str, Any] | None:
    consent = await announce_result_consent(
        parsed,
        connected_consent_announcer=connected_consent_announcer,
        agent_consent_announcer=agent_consent_announcer,
    )
    if consent is not None:
        return consent
    if file_deliverer is None:
        return None
    delivered = await file_deliverer(parsed)
    return delivered if delivered is not parsed else None


async def _process_text(
    text: str,
    *,
    connected_consent_announcer: ConnectedConsentAnnouncer | None,
    agent_consent_announcer: AgentConsentAnnouncer | None,
    file_deliverer: ResultFileDeliverer | None,
) -> str | None:
    parsed = _postprocessable(text)
    if parsed is None:
        return None
    replaced = await _process_dict(
        parsed,
        connected_consent_announcer=connected_consent_announcer,
        agent_consent_announcer=agent_consent_announcer,
        file_deliverer=file_deliverer,
    )
    return json.dumps(replaced, ensure_ascii=False) if replaced is not None else None


async def _process_content(
    content: Any,
    *,
    connected_consent_announcer: ConnectedConsentAnnouncer | None,
    agent_consent_announcer: AgentConsentAnnouncer | None,
    file_deliverer: ResultFileDeliverer | None,
) -> Any | None:
    # MCP bindings may return a string, a structured dict, or content blocks.
    if isinstance(content, str):
        return await _process_text(
            content,
            connected_consent_announcer=connected_consent_announcer,
            agent_consent_announcer=agent_consent_announcer,
            file_deliverer=file_deliverer,
        )
    if isinstance(content, dict):
        direct = await _process_dict(
            content,
            connected_consent_announcer=connected_consent_announcer,
            agent_consent_announcer=agent_consent_announcer,
            file_deliverer=file_deliverer,
        )
        if direct is not None:
            return direct
        if content.get("type") == "text" and isinstance(content.get("text"), str):
            new_text = await _process_text(
                content["text"],
                connected_consent_announcer=connected_consent_announcer,
                agent_consent_announcer=agent_consent_announcer,
                file_deliverer=file_deliverer,
            )
            return {**content, "text": new_text} if new_text is not None else None
        nested = content.get("content")
        if isinstance(nested, (str, dict, list)):
            replaced = await _process_content(
                nested,
                connected_consent_announcer=connected_consent_announcer,
                agent_consent_announcer=agent_consent_announcer,
                file_deliverer=file_deliverer,
            )
            return {**content, "content": replaced} if replaced is not None else None
        return None
    if isinstance(content, list):
        changed = False
        out: List[Any] = []
        for item in content:
            replacement = None
            if isinstance(item, str):
                replacement = await _process_text(
                    item,
                    connected_consent_announcer=connected_consent_announcer,
                    agent_consent_announcer=agent_consent_announcer,
                    file_deliverer=file_deliverer,
                )
            elif isinstance(item, Mapping) and item.get("type") == "text" and isinstance(item.get("text"), str):
                new_text = await _process_text(
                    item["text"],
                    connected_consent_announcer=connected_consent_announcer,
                    agent_consent_announcer=agent_consent_announcer,
                    file_deliverer=file_deliverer,
                )
                if new_text is not None:
                    replacement = {**item, "text": new_text}
            if replacement is not None:
                out.append(replacement)
                changed = True
            else:
                out.append(item)
        return out if changed else None
    return None


def _unprocessed_sentinel(result: Any) -> None:
    # Loud when a result CARRIES a consent denial or download URL but was not
    # post-processed (unhandled shape) — the user saw nothing.
    raw = repr(result)
    for marker in (DELEGATED_CONSENT_REQUIRED, NEEDS_CONNECTED_ACCOUNT_CONSENT, "download_token"):
        if marker in raw:
            logger.warning(
                "[mcp-result] result carries %r but was NOT post-processed (shape %s) — "
                "no banner/file card reached the user",
                marker, type(result).__name__,
            )
            return


def bind_chat_result_handling(
    tools: List[Any],
    *,
    connected_consent_announcer: ConnectedConsentAnnouncer | None = None,
    agent_consent_announcer: AgentConsentAnnouncer | None = None,
    file_deliverer: ResultFileDeliverer | None = None,
) -> List[Any]:
    """Wrap each MCP tool so its result is post-processed for the chat surface:
    consent denials become banners, files become cards, both through shared SDK.
    Applied ONCE by the loader; every consumer inherits it. Mutates each tool's
    coroutine in place; tools without one pass through."""
    def _wrap(orig: Any) -> Any:
        async def run(*args: Any, **kwargs: Any) -> Any:
            result = await orig(*args, **kwargs)
            try:
                if isinstance(result, tuple) and len(result) == 2:
                    replaced = await _process_content(
                        result[0],
                        connected_consent_announcer=connected_consent_announcer,
                        agent_consent_announcer=agent_consent_announcer,
                        file_deliverer=file_deliverer,
                    )
                    if replaced is not None:
                        return (replaced, result[1])
                else:
                    replaced = await _process_content(
                        result,
                        connected_consent_announcer=connected_consent_announcer,
                        agent_consent_announcer=agent_consent_announcer,
                        file_deliverer=file_deliverer,
                    )
                    if replaced is not None:
                        return replaced
                _unprocessed_sentinel(result)
            except Exception:  # pragma: no cover
                logger.warning("[mcp-result] post-process failed (non-fatal)", exc_info=True)
            return result

        return run

    wrapped = 0
    for tool in tools or []:
        orig = getattr(tool, "coroutine", None)
        if callable(orig):
            try:
                tool.coroutine = _wrap(orig)
                wrapped += 1
            except Exception:
                logger.info("[mcp-result] wrap failed for tool %s", getattr(tool, "name", "?"))
        else:
            logger.warning(
                "[mcp-result] tool %s has no coroutine — consent/delivery post-processing will NOT run for it",
                getattr(tool, "name", "?"),
            )
    if tools:
        logger.info("[mcp-result] %d/%d MCP tools bound for chat consent+delivery post-processing", wrapped, len(tools))
    return tools


__all__ = [
    "AgentConsentAnnouncer",
    "ConnectedConsentAnnouncer",
    "ResultFileDeliverer",
    "announce_result_consent",
    "bind_chat_result_handling",
]
