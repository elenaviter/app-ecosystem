from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit

from app_foundation.data_bus import (
    DataBusClientError,
    DataBusIngressRejected,
    DataBusOutcomeUnknown,
    FederatedDataBusClient,
)

from ..contract.errors import DomainError
from ..contract.operation_outcomes import require_successful_operation_envelope
from .relay_failures import is_timeout_error, safe_target


logger = logging.getLogger(__name__)


class RemoteTools(Protocol):
    async def call_tool(
        self,
        *,
        name: str,
        arguments: dict[str, Any] | None,
        input_responses: Any,
        request_state: str | None,
        meta: Any,
        progress_callback: Any,
    ) -> Any: ...


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _refusal(payload: Mapping[str, Any], fallback_code: str, fallback_message: str) -> tuple[str, str]:
    """The code and sentence a refusal actually carries.

    A governed refusal nests its real error: {"ok": false, "error": {"code":
    ..., "message": ...}}. Reading payload["error"] straight into str() turned
    that mapping into a Python repr, so the code came back as
    "{'code': 'delegated_capability_no_longer_available', ...}" and the message
    was a JSON blob. The caller then had no code to branch on and nothing a
    person could read, which defeats the point of a refusal saying what it
    wanted.
    """

    error = payload.get("error")
    if isinstance(error, Mapping):
        code = str(error.get("code") or fallback_code)
        message = str(error.get("message") or fallback_message)
    elif isinstance(error, str) and error.strip():
        code = error
        message = str(payload.get("message") or fallback_message)
    else:
        code = str(payload.get("code") or fallback_code)
        message = str(payload.get("message") or fallback_message)

    # Name what was asked for. Without this the reader knows a call was refused
    # and never which operation or resource, which is the difference between a
    # refusal they can act on and one they can only report.
    wanted = payload.get("ret")
    wanted = wanted.get("requested_capability") if isinstance(wanted, Mapping) else None
    if isinstance(wanted, Mapping):
        operation = str(wanted.get("outer_operation") or wanted.get("operation") or "")
        if operation and operation not in message:
            message = f"{message} Operation: {operation}."
    return code, message


def _tool_payload(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    payload = _mapping(structured)
    if not payload:
        for block in getattr(result, "content", None) or []:
            text = getattr(block, "text", None)
            if not isinstance(text, str):
                continue
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, Mapping):
                payload = dict(decoded)
                break
    if getattr(result, "isError", False) or getattr(result, "is_error", False):
        # A failure whose body is not JSON used to arrive as an empty detail
        # dict and the message "the call failed", which tells the caller
        # nothing it can act on. Keep the text the server actually sent.
        texts = [
            text
            for block in getattr(result, "content", None) or []
            for text in [getattr(block, "text", None)]
            if isinstance(text, str) and text.strip()
        ]
        code, message = _refusal(
            payload,
            "work_mcp_tool_failed",
            " ".join(texts) or "The Problem Board MCP call failed.",
        )
        raise DomainError(
            code,
            message,
            status=int(payload.get("status") or 502),
            details=payload or ({"server_text": texts} if texts else {}),
        )
    if payload.get("ok") is False:
        code, message = _refusal(
            payload,
            "work_remote_denied",
            "The Problem Board control plane denied the call.",
        )
        raise DomainError(code, message, status=int(payload.get("status") or 403), details=payload)
    require_successful_operation_envelope(
        str(payload.get("operation") or ""),
        payload,
    )
    return payload


class ProblemBoardMcpClient:
    """Call canonical operations on the governed Problem Board MCP surface."""

    def __init__(self, remote: RemoteTools) -> None:
        self.remote = remote

    async def action(
        self,
        *,
        object_ref: str,
        action: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = await self.remote.call_tool(
            name=str(action or ""),
            arguments={
                "object_ref": str(object_ref or ""),
                "payload": dict(payload or {}),
            },
            input_responses=None,
            request_state=None,
            meta=None,
            progress_callback=None,
        )
        return _tool_payload(result)


def governed_endpoint_identity(endpoint: str) -> tuple[str, str]:
    """Return the ``(bundle_id, card_resource)`` this governed endpoint names.

    The card resource is the endpoint itself, not a shortened form of it. A
    card records its grants against a pattern written over the whole path:

        */api/integrations/bundles/*/*/<bundle>/public/mcp/<service>*

    so ``<bundle>/public/mcp/<service>`` on its own matches nothing and every
    connect would be refused. This is the shape the catalog already declares;
    the endpoint is simply presented as it stands.

    The bundle id is still read off the path rather than configured beside it,
    because a configured copy drifts. Renaming the service changes the endpoint
    and the resource together, with nothing else to keep in step.
    """

    parts = [part for part in urlsplit(endpoint).path.split("/") if part]
    try:
        marker = parts.index("public")
    except ValueError:
        marker = -1
    tail = parts[marker : marker + 3] if marker >= 1 else []
    if tail != ["public", "mcp", "problem_board"]:
        raise DomainError(
            "work_relay_endpoint_not_governed",
            "The configured endpoint is not the governed Problem Board MCP address.",
            status=409,
            details={"endpoint": str(endpoint or "")},
        )
    return parts[marker - 1], str(endpoint or "").strip().rstrip("/")


class ProblemBoardDataBusClient:
    """Send canonical Problem Board operations through correlated Data Bus."""

    subject = "problem_board.command.v1"

    def __init__(
        self,
        client: FederatedDataBusClient,
        *,
        partition_ref: str,
    ) -> None:
        self.client = client
        self.partition_ref = str(partition_ref or "").strip()
        if not self.partition_ref:
            raise ValueError("partition_ref is required")
        self._uncertain: dict[str, tuple[str, str]] = {}

    @property
    def connected(self) -> bool:
        return self.client.connected

    async def wait_until_connected(self, timeout_seconds: float) -> bool:
        """Wait for the socket's own reconnect, up to ``timeout_seconds``."""

        wait = getattr(self.client, "wait_until_connected", None)
        if callable(wait):
            return bool(await wait(timeout_seconds))
        return bool(self.connected)

    @staticmethod
    def _fingerprint(
        *, object_ref: str, action: str, payload: Mapping[str, Any]
    ) -> str:
        encoded = json.dumps(
            {
                "action": str(action),
                "object_ref": str(object_ref),
                "payload": dict(payload),
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _transport_evidence(
        *, action: str, object_ref: str, message_id: str,
        phase: str, started: float, replayed: bool,
        ingress_accepted: bool | None = None,
        project_ref: str = "",
    ) -> dict[str, Any]:
        evidence: dict[str, Any] = {
            "operation": str(action),
            "target": safe_target(object_ref),
            "transport_message_id": str(message_id),
            "transport_phase": phase,
            "transport_replayed": bool(replayed),
            "elapsed_seconds": round(max(0.0, time.monotonic() - started), 3),
        }
        if ingress_accepted is not None:
            evidence["ingress_accepted"] = ingress_accepted
        if project_ref:
            evidence["request_scope"] = project_ref
        return evidence

    async def action(
        self,
        *,
        object_ref: str,
        action: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._action(
            object_ref=object_ref,
            action=action,
            payload=payload,
            transport_request_id="",
        )

    async def action_with_transport_identity(
        self,
        *,
        object_ref: str,
        action: str,
        payload: Mapping[str, Any] | None = None,
        transport_request_id: str,
    ) -> dict[str, Any]:
        """Replay one relay-queued request with the same Data Bus identity."""

        request_id = str(transport_request_id or "").strip()
        if not request_id:
            raise ValueError("transport_request_id is required")
        return await self._action(
            object_ref=object_ref,
            action=action,
            payload=payload,
            transport_request_id=request_id,
        )

    async def _action(
        self,
        *,
        object_ref: str,
        action: str,
        payload: Mapping[str, Any] | None,
        transport_request_id: str,
    ) -> dict[str, Any]:
        value = dict(payload or {})
        fingerprint = self._fingerprint(
            object_ref=object_ref, action=action, payload=value
        )
        if transport_request_id:
            message_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"problem-board-coordinate:{transport_request_id}",
                )
            )
            idempotency_key = "problem-board:" + hashlib.sha256(
                f"coordinate:{transport_request_id}".encode("utf-8")
            ).hexdigest()
            attempt = (message_id, idempotency_key)
            replayed = False
        else:
            attempt = self._uncertain.get(fingerprint)
            replayed = attempt is not None
        if attempt is None:
            message_id = str(uuid.uuid4())
            domain_key = str(value.get("idempotency_key") or "").strip()
            identity = f"{action}|{object_ref}|{domain_key}|{message_id}".encode(
                "utf-8"
            )
            idempotency_key = f"problem-board:{hashlib.sha256(identity).hexdigest()}"
            attempt = (message_id, idempotency_key)
        message_id, idempotency_key = attempt
        started = time.monotonic()
        request_scope = "project" if value.get("project_ref") else "worker"
        try:
            outcome = await self.client.request(
                subject=self.subject,
                object_ref=self.partition_ref,
                payload={
                    "operation": str(action or ""),
                    "object_ref": str(object_ref or ""),
                    "payload": value,
                },
                idempotency_key=idempotency_key,
                message_id=message_id,
            )
        except DataBusOutcomeUnknown as exc:
            if not transport_request_id:
                self._uncertain[fingerprint] = attempt
            evidence = self._transport_evidence(
                action=action, object_ref=object_ref, message_id=message_id,
                phase="outcome.wait" if exc.accepted else "ingress.ack",
                started=started, replayed=replayed,
                ingress_accepted=True if exc.accepted else None,
                project_ref=request_scope,
            )
            raise DomainError(
                exc.code,
                str(exc),
                status=504,
                details={**dict(exc.details), **evidence},
            ) from exc
        except DataBusIngressRejected as exc:
            if not transport_request_id:
                self._uncertain.pop(fingerprint, None)
            rejection_type = str(exc.details.get("error_type") or "")
            try:
                status = int(exc.details.get("status") or 400)
            except (TypeError, ValueError):
                status = 400
            code = {
                "federated_token_expired": "work_relay_stream_expired",
                "delegated_card_expired": "delegated_card_expired",
                "delegated_card_not_active": "delegated_card_not_active",
                "delegated_resource_not_granted": "delegated_resource_not_granted",
                "delegated_card_scope_invalid": "delegated_card_scope_invalid",
                "delegated_card_scope_mismatch": "delegated_card_scope_mismatch",
                "delegated_card_unavailable": "work_relay_transport_unavailable",
            }.get(rejection_type, exc.code)
            raise DomainError(
                code,
                str(exc),
                status=status,
                details={
                    **dict(exc.details),
                    **self._transport_evidence(
                        action=action, object_ref=object_ref, message_id=message_id,
                        phase="ingress.rejected", started=started,
                        replayed=replayed, ingress_accepted=False,
                        project_ref=request_scope,
                    ),
                },
            ) from exc
        except DataBusClientError as exc:
            raise DomainError(
                "work_relay_transport_unavailable",
                str(exc),
                status=503,
                details={
                    **dict(exc.details),
                    "transport_code": exc.code,
                    **self._transport_evidence(
                        action=action, object_ref=object_ref, message_id=message_id,
                        phase="transport.error", started=started,
                        replayed=replayed, project_ref=request_scope,
                    ),
                },
            ) from exc
        except Exception as exc:
            if not is_timeout_error(exc):
                raise
            if not transport_request_id:
                self._uncertain[fingerprint] = attempt
            socketio_ingress_timeout = (
                type(exc).__name__ == "TimeoutError"
                and type(exc).__module__.startswith("socketio")
            )
            raise DomainError(
                "data_bus_outcome_unknown",
                "The governed operation timed out without a terminal receipt; it may have completed.",
                status=504,
                details={
                    "message_id": message_id,
                    "cause_type": type(exc).__name__,
                    **self._transport_evidence(
                        action=action, object_ref=object_ref, message_id=message_id,
                        phase=(
                            "ingress.ack" if socketio_ingress_timeout
                            else "transport.unknown"
                        ),
                        started=started,
                        replayed=replayed, project_ref=request_scope,
                    ),
                },
            ) from exc
        if not transport_request_id:
            self._uncertain.pop(fingerprint, None)
        if replayed:
            logger.info(
                "Problem Board Data Bus retry resolved operation=%s target=%s "
                "transport_message_id=%s status=%s elapsed_seconds=%.3f",
                action, safe_target(object_ref), message_id, outcome.status,
                max(0.0, time.monotonic() - started),
            )
        if outcome.status == "error" or outcome.error is not None:
            error = dict(outcome.error or {})
            details = _mapping(error.get("details"))
            try:
                status = int(details.pop("domain_status", 500))
            except (TypeError, ValueError):
                status = 500
            raise DomainError(
                str(error.get("code") or "work_remote_denied"),
                str(error.get("message") or "Problem Board rejected the operation."),
                status=status,
                details=details,
            )
        if outcome.status == "conflict":
            raise DomainError(
                "work_remote_conflict",
                "Problem Board could not apply the operation to its current state.",
                status=409,
                details=dict(outcome.data),
            )
        result = dict(outcome.data)
        require_successful_operation_envelope(str(action or ""), result)
        return result

    async def wait_for_event(self, timeout_seconds: float) -> dict[str, Any] | None:
        return await self.client.wait_for_event(timeout_seconds)


__all__ = [
    "ProblemBoardDataBusClient",
    "ProblemBoardMcpClient",
    "governed_endpoint_identity",
]
