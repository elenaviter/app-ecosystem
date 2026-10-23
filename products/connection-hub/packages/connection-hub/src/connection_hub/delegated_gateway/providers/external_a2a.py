# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Provider adapter for user-owned remote A2A connectors.

Each pinned skill is one operation, ``send_<skill>``; ``get_task``,
``cancel_task`` and ``reply_task`` act on a task this Card sent. Every
operation is its own Card operation with its own invocation policy, so a
Once on a send is spent by that send only, and polling the task does not ask
again.

What the outside agent says stays data. A failed, rejected or auth-required
task, and every client failure, becomes one fixed code with no remote text;
only an input-required question is passed on, as the task's bounded status
text. The gateway, not this provider, consumes invocation policies.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from typing import Any

from app_foundation.a2a import A2AClientError, A2ATask

from connection_hub.delegated_gateway.models import (
    DESCRIPTOR_CURRENT,
    RESOURCE_DISABLED,
    ProviderCallAdmission,
    ProviderCallResult,
    ProviderDescriptor,
    ProviderTool,
)
from connection_hub.delegated_gateway.ports import GatewayProviderContext
from connection_hub.remote_a2a.catalog import REMOTE_A2A_RESOURCE_KIND
from connection_hub.remote_a2a.models import (
    CANCEL_TASK_OPERATION,
    EXTERNAL_A2A_GRANT,
    GET_TASK_OPERATION,
    REPLY_TASK_OPERATION,
    SEND_OPERATION_PREFIX,
    TASK_OPERATIONS,
    RemoteA2AConnector,
    RemoteA2ARecordError,
    connector_id_from_resource,
    operation_digest,
)
from connection_hub.remote_a2a.service import (
    RemoteA2AConnectorConflict,
    RemoteA2AConnectorNotFound,
    RemoteA2AConnectorService,
    RemoteA2AContextNotFound,
    RemoteA2ATaskNotFound,
)
from connection_hub.remote_mcp.models import CONNECTOR_ACTIVE

EXTERNAL_A2A_PROVIDER_ID = "remote_a2a"
MAX_TEXT_CHARS = 16_000
MAX_ID_CHARS = 128

REMOTE_TASK_FAILED = "remote_task_failed"
REMOTE_TASK_REJECTED = "remote_task_rejected"
REMOTE_AUTH_REQUIRED = "remote_auth_required"
SERVICE_UNAVAILABLE = "service_unavailable"
A2A_TASK_NOT_FOUND = "a2a_task_not_found"
A2A_CONTEXT_NOT_FOUND = "a2a_context_not_found"
# One live card observation serves admission and dispatch of one invocation.
OBSERVATION_TTL_SECONDS = 30.0
OBSERVATION_CACHE_SIZE = 256

_TASK_STATE_ERRORS = {
    "TASK_STATE_FAILED": REMOTE_TASK_FAILED,
    "TASK_STATE_REJECTED": REMOTE_TASK_REJECTED,
    "TASK_STATE_AUTH_REQUIRED": REMOTE_AUTH_REQUIRED,
}
_CLIENT_ERRORS = {
    "remote_authorization_rejected": REMOTE_AUTH_REQUIRED,
    "remote_task_not_found": A2A_TASK_NOT_FOUND,
}

_TEXT = {"type": "string", "minLength": 1, "maxLength": MAX_TEXT_CHARS}
_DATA = {"type": "object"}
_ID = {"type": "string", "minLength": 1, "maxLength": MAX_ID_CHARS}
_SCHEMAS: dict[str, dict[str, Any]] = {
    "send": {
        "type": "object",
        "properties": {"text": _TEXT, "data": _DATA, "context_id": _ID},
        "required": ["text"],
        "additionalProperties": False,
    },
    GET_TASK_OPERATION: {
        "type": "object",
        "properties": {"task_id": _ID},
        "required": ["task_id"],
        "additionalProperties": False,
    },
    CANCEL_TASK_OPERATION: {
        "type": "object",
        "properties": {"task_id": _ID},
        "required": ["task_id"],
        "additionalProperties": False,
    },
    REPLY_TASK_OPERATION: {
        "type": "object",
        "properties": {"task_id": _ID, "text": _TEXT, "data": _DATA},
        "required": ["task_id", "text"],
        "additionalProperties": False,
    },
}
_TASK_DESCRIPTIONS = {
    GET_TASK_OPERATION: "Read the state and results of a task you sent to this agent.",
    CANCEL_TASK_OPERATION: "Cancel a task you sent to this agent.",
    REPLY_TASK_OPERATION: "Answer a task you sent to this agent that is waiting for input.",
}


def _schema_for(operation: str) -> dict[str, Any]:
    return _SCHEMAS["send" if operation.startswith(SEND_OPERATION_PREFIX) else operation]


def _arguments_valid(operation: str, arguments: Mapping[str, Any]) -> bool:
    schema = _schema_for(operation)
    if not isinstance(arguments, Mapping) or set(arguments) - set(schema["properties"]):
        return False
    for name in schema["required"]:
        if name not in arguments:
            return False
    for name, value in arguments.items():
        rule = schema["properties"][name]
        if rule["type"] == "string":
            if not isinstance(value, str) or not value.strip() or len(value) > rule["maxLength"]:
                return False
        elif not isinstance(value, Mapping):
            return False
    return True


def _task_result(task: A2ATask) -> ProviderCallResult:
    code = _TASK_STATE_ERRORS.get(task.state)
    if code is not None:
        return ProviderCallResult(
            structured_content={"ok": False, "error": code, "task_id": task.task_id, "state": task.state},
            is_error=True,
        )
    return ProviderCallResult(
        structured_content={
            "ok": True,
            "task_id": task.task_id,
            "context_id": task.context_id,
            "state": task.state,
            # Only a question the agent needs answered is passed on, as data.
            "status_text": task.status_text if task.state == "TASK_STATE_INPUT_REQUIRED" else "",
            "artifacts": [
                {
                    "name": artifact.name,
                    "parts": [
                        {"text": part.text} if part.text is not None else {"data": part.data}
                        for part in artifact.parts
                    ],
                }
                for artifact in task.artifacts
            ],
            "dropped_parts": task.dropped_parts,
        }
    )


def _failure(code: str) -> ProviderCallResult:
    return ProviderCallResult(structured_content={"ok": False, "error": code}, is_error=True)


class ExternalRemoteA2AProvider:
    def __init__(self, service: RemoteA2AConnectorService, *, clock: Any = time.monotonic) -> None:
        self._service = service
        self._clock = clock
        self._observed: OrderedDict[tuple[str, int, str], tuple[float, Any]] = OrderedDict()

    async def _observe(self, connector: RemoteA2AConnector, invocation_id: str) -> Any:
        """The live card, fetched once per invocation (admission, then call)."""

        if not invocation_id:
            return await self._service.observe(connector)
        key = (connector.connector_id, int(connector.revision), invocation_id)
        now = self._clock()
        cached = self._observed.get(key)
        if cached is not None and now - cached[0] <= OBSERVATION_TTL_SECONDS:
            return cached[1]
        live = await self._service.observe(connector)
        self._observed[key] = (now, live)
        self._observed.move_to_end(key)
        while len(self._observed) > OBSERVATION_CACHE_SIZE:
            self._observed.popitem(last=False)
        return live

    @property
    def provider_id(self) -> str:
        return EXTERNAL_A2A_PROVIDER_ID

    @property
    def resource_kinds(self) -> frozenset[str]:
        return frozenset({REMOTE_A2A_RESOURCE_KIND})

    async def _connector(self, context: GatewayProviderContext) -> RemoteA2AConnector:
        connector_id = connector_id_from_resource(context.resource.resource_id)
        if not connector_id:
            raise ValueError("remote_a2a_resource_invalid")
        return await self._service.get(owner_subject=context.card.grantor_subject, connector_id=connector_id)

    async def current_descriptor(self, context: GatewayProviderContext) -> ProviderDescriptor:
        connector = await self._connector(context)
        active = connector.state == CONNECTOR_ACTIVE
        digest = connector.card_digest
        operations = connector.operations()
        if active:
            # Readiness and drift: the live card, through the endpoint policy.
            live = await self._service.observe(connector)
            digest = live.digest
            operations = tuple(f"{SEND_OPERATION_PREFIX}{skill.skill_id}" for skill in live.skills) + TASK_OPERATIONS
        return ProviderDescriptor(
            resource_id=context.resource.resource_id,
            revision=str(connector.descriptor_revision),
            digest=digest,
            operation_digests={operation: operation_digest(digest, operation) for operation in operations},
            state=DESCRIPTOR_CURRENT if active else RESOURCE_DISABLED,
            unavailable_reason="" if active else "connector_not_active",
        )

    def _granted(self, context: GatewayProviderContext) -> bool:
        return EXTERNAL_A2A_GRANT in tuple(context.resource.grants)

    async def list_tools(self, context: GatewayProviderContext) -> Sequence[ProviderTool]:
        try:
            connector = await self._connector(context)
        except (RemoteA2AConnectorNotFound, ValueError):
            return ()
        if connector.state != CONNECTOR_ACTIVE or connector.card is None or not self._granted(context):
            return ()
        selected = set(context.resource.operations)
        tools: list[ProviderTool] = []
        for skill in connector.card.skills:
            operation = f"{SEND_OPERATION_PREFIX}{skill.skill_id}"
            if operation in selected:
                tools.append(
                    ProviderTool(
                        operation=operation,
                        descriptor_digest=operation_digest(connector.card_digest, operation),
                        title=skill.name,
                        description=skill.description,
                        input_schema=_schema_for(operation),
                    )
                )
        for operation in TASK_OPERATIONS:
            if operation in selected:
                tools.append(
                    ProviderTool(
                        operation=operation,
                        descriptor_digest=operation_digest(connector.card_digest, operation),
                        title=operation.replace("_", " ").capitalize(),
                        description=_TASK_DESCRIPTIONS[operation],
                        input_schema=_schema_for(operation),
                    )
                )
        return tuple(tools)

    async def _admit(
        self, context: GatewayProviderContext, operation: str, arguments: Mapping[str, Any], invocation_id: str = ""
    ) -> tuple[RemoteA2AConnector | None, str]:
        try:
            connector = await self._connector(context)
        except (RemoteA2AConnectorNotFound, ValueError):
            return None, "tool_not_in_delegated_resources"
        if connector.state != CONNECTOR_ACTIVE or connector.card is None:
            return None, "connector_not_active"
        if not self._granted(context):
            return None, "connector_grant_not_consented"
        if operation not in set(context.resource.operations):
            return None, "operation_not_consented"
        if operation not in connector.operations():
            return None, "operation_removed_by_server"
        live = await self._observe(connector, invocation_id)
        if live.digest != connector.card_digest:
            return None, "operation_descriptor_changed"
        if not _arguments_valid(operation, arguments):
            return None, "operation_arguments_invalid"
        if operation.startswith(SEND_OPERATION_PREFIX) and arguments.get("context_id"):
            try:
                await self._service.context_owned_by(
                    connector=connector, access_id=context.card.access_id, context_id=str(arguments["context_id"])
                )
            except RemoteA2AContextNotFound:
                return None, A2A_CONTEXT_NOT_FOUND
        if operation in TASK_OPERATIONS:
            try:
                await self._service.task_owned_by(
                    connector=connector, access_id=context.card.access_id, task_id=str(arguments.get("task_id"))
                )
            except RemoteA2ATaskNotFound:
                return None, A2A_TASK_NOT_FOUND
        return connector, ""

    async def admit_call(
        self,
        context: GatewayProviderContext,
        *,
        operation: str,
        arguments: Mapping[str, Any],
        invocation_id: str,
    ) -> ProviderCallAdmission:
        try:
            _connector, reason = await self._admit(context, operation, arguments, invocation_id)
        except (A2AClientError, RemoteA2AConnectorConflict, RemoteA2ARecordError):
            return ProviderCallAdmission(allowed=False, reason=SERVICE_UNAVAILABLE, retryable=True)
        if reason:
            return ProviderCallAdmission(allowed=False, reason=reason, retryable=False)
        return ProviderCallAdmission(allowed=True)

    async def call_tool(
        self,
        context: GatewayProviderContext,
        *,
        operation: str,
        arguments: Mapping[str, Any],
        invocation_id: str,
    ) -> ProviderCallResult:
        try:
            connector, reason = await self._admit(context, operation, arguments, invocation_id)
            if connector is None:
                return _failure(reason)
            access_id = context.card.access_id
            data = arguments.get("data")
            if operation.startswith(SEND_OPERATION_PREFIX):
                task = await self._service.send(
                    connector=connector,
                    access_id=access_id,
                    skill_id=operation[len(SEND_OPERATION_PREFIX) :],
                    text=str(arguments["text"]),
                    data=data if isinstance(data, Mapping) else None,
                    context_id=str(arguments.get("context_id") or ""),
                )
            elif operation == GET_TASK_OPERATION:
                task = await self._service.get_task(connector=connector, access_id=access_id, task_id=str(arguments["task_id"]))
            elif operation == CANCEL_TASK_OPERATION:
                task = await self._service.cancel_task(connector=connector, access_id=access_id, task_id=str(arguments["task_id"]))
            else:
                task = await self._service.reply(
                    connector=connector,
                    access_id=access_id,
                    task_id=str(arguments["task_id"]),
                    text=str(arguments["text"]),
                    data=data if isinstance(data, Mapping) else None,
                )
        except A2AClientError as exc:
            return _failure(_CLIENT_ERRORS.get(exc.code, SERVICE_UNAVAILABLE))
        except RemoteA2ATaskNotFound:
            return _failure(A2A_TASK_NOT_FOUND)
        except RemoteA2AContextNotFound:
            return _failure(A2A_CONTEXT_NOT_FOUND)
        except (RemoteA2AConnectorConflict, RemoteA2ARecordError):
            return _failure(SERVICE_UNAVAILABLE)
        return _task_result(task)


__all__ = ["EXTERNAL_A2A_PROVIDER_ID", "ExternalRemoteA2AProvider"]
