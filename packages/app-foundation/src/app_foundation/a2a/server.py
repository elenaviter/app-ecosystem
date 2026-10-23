# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""An inbound A2A 1.0 server over a host-supplied target.

The protocol is the a2a-sdk's: its JSON-RPC and HTTP+JSON dispatchers parse
requests and render responses, including 0.3 compatibility for JSON-RPC.
Everything behind them is a :class:`A2ATarget` the host supplies, so a task can
be a board item, a chat turn or an application operation without this module
knowing which.

What this module owns, and the host never re-implements:

- the Agent Card, served unauthenticated with an ETag;
- the methods offered in this version (SendMessage, GetTask, CancelTask,
  ListTasks); streaming, subscriptions and push answer "unsupported";
- inbound parts: text and data kept, file parts capped, URL parts refused,
  message metadata ignored (authority never comes from a payload);
- per-client idempotency: a client's ``messageId`` is namespaced by the
  published agent and the client's Card before the target sees it;
- rate limits, counted through a :class:`A2ALimiter` the host supplies;
- errors: only fixed codes and texts leave; nothing a target raises does.

Authentication is not here. The host admits the caller (for KDCube, the
Connection Hub guard with its entry binding) and passes an :class:`A2ACaller`.
The entry check below is defence in depth only.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Protocol, Sequence
from urllib.parse import urlsplit

from a2a.server.context import ServerCallContext
from a2a.server.request_handlers.request_handler import RequestHandler
from a2a.server.routes.common import DefaultServerCallContextBuilder, ServerCallContextBuilder
from a2a.server.routes.jsonrpc_dispatcher import JsonRpcDispatcher
from a2a.server.routes.rest_dispatcher import RestDispatcher
from a2a.types import a2a_pb2 as pb
from a2a.utils.errors import (
    ExtendedAgentCardNotConfiguredError,
    InternalError,
    InvalidParamsError,
    PushNotificationNotSupportedError,
    TaskNotCancelableError,
    TaskNotFoundError,
    UnsupportedOperationError,
)
from google.protobuf.json_format import MessageToDict
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app_foundation.a2a.card import AgentSkill, bounded_text
from app_foundation.a2a.client import PROTOCOL_VERSION, TASK_STATES

A2A_SERVER_ERROR_CODES = (
    "a2a_agent_unpublished",
    "a2a_disabled",
    "a2a_entry_mismatch",
    "a2a_not_found",
    "a2a_part_refused",
    "a2a_rate_limited",
    "a2a_task_not_found",
    "a2a_unauthenticated",
)
TARGET_ERROR_CODES = ("task_not_found", "task_not_cancelable", "task_closed", "disabled", "unpublished")

CARD_TAILS = ("agent-card.json", "agent.json")
SCHEME_NAME = "connection_hub"
_SEND_METHODS = {"SendMessage", "message/send", "SendStreamingMessage", "message/stream"}
_CALLER_STATE = "a2a_caller"


# --- what a host supplies ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class A2ACaller:
    """The admitted caller, from the host's authentication only."""

    principal: str  # the consenting person: the task's requester
    client_label: str
    card_access_id: str  # the client's Card: tasks, contexts and idempotency are per Card
    entry_resource: str  # the URL the Card was issued for (defence-in-depth check)


@dataclass(frozen=True, slots=True)
class PublishedAgent:
    """What the Agent Card says. ``base_url`` is the agent's own A2A URL."""

    slug: str
    name: str
    description: str
    version: str
    base_url: str
    skills: tuple[AgentSkill, ...]
    authorization_url: str
    token_url: str
    scopes: tuple[str, ...] = ()
    oauth_metadata_url: str = ""
    provider_organization: str = ""
    provider_url: str = ""


@dataclass(frozen=True, slots=True)
class PartLimits:
    max_parts: int = 32
    max_text_chars: int = 100_000
    max_data_bytes: int = 256 * 1024
    max_part_bytes: int = 2 * 1024 * 1024
    max_task_bytes: int = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class RateLimits:
    """Requests per hour that send a message. A host may lower them per agent, never raise them."""

    per_client_per_hour: int = 60
    per_agent_per_hour: int = 600


class A2ALimiter(Protocol):
    async def admit(self, counters: Sequence[tuple[str, int]], *, window_seconds: int) -> int:
        """Count one request against every ``(key, limit)``, all or nothing.

        Returns 0 when admitted, otherwise the seconds until the earliest
        refusing window ends; a refused request is counted nowhere.
        """


@dataclass(frozen=True, slots=True)
class InboundPart:
    kind: str  # "text" | "data" | "file"
    text: str = ""
    data: Any = None
    filename: str = ""
    media_type: str = ""
    raw: bytes = b""


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """Outside content: data, never instructions. ``idempotency_key`` is per agent and client Card."""

    message_id: str
    idempotency_key: str
    context_id: str
    task_id: str
    parts: tuple[InboundPart, ...]


@dataclass(frozen=True, slots=True)
class OutboundPart:
    text: str = ""
    raw: bytes = b""
    filename: str = ""
    media_type: str = ""


@dataclass(frozen=True, slots=True)
class TaskArtifactView:
    artifact_id: str
    name: str
    parts: tuple[OutboundPart, ...]


@dataclass(frozen=True, slots=True)
class TaskView:
    """What a client may see of one task: only text and files written for it."""

    task_id: str
    context_id: str
    state: str
    status_text: str = ""
    artifacts: tuple[TaskArtifactView, ...] = ()

    def __post_init__(self) -> None:
        if self.state not in TASK_STATES:
            raise ValueError("task_state_invalid")


@dataclass(frozen=True, slots=True)
class TaskPage:
    tasks: tuple[TaskView, ...]
    next_page_token: str = ""


class A2ATargetError(Exception):
    """A target's refusal, by fixed code. Its text never reaches the client."""

    def __init__(self, code: str) -> None:
        if code not in TARGET_ERROR_CODES:
            raise ValueError("target_error_code_invalid")
        super().__init__(code)
        self.code = code


class A2ATarget(Protocol):
    """One published agent's tasks. Every call is for ``caller``'s Card only.

    ``read``, ``cancel``, ``follow_up`` and ``list`` see only tasks this Card
    created; another Card's task is "not found". A ``context_id`` another Card
    created is never continued.
    """

    async def submit(self, caller: A2ACaller, message: InboundMessage) -> TaskView: ...

    async def follow_up(self, caller: A2ACaller, task_id: str, message: InboundMessage) -> TaskView: ...

    async def read(self, caller: A2ACaller, task_id: str) -> TaskView | None: ...

    async def cancel(self, caller: A2ACaller, task_id: str) -> TaskView | None: ...

    async def list(self, caller: A2ACaller, *, context_id: str, page_size: int, page_token: str) -> TaskPage: ...


# --- the Agent Card ----------------------------------------------------------------------------


def build_agent_card(agent: PublishedAgent) -> dict[str, Any]:
    """The public Agent Card JSON (A2A 1.0, camelCase)."""

    scopes = {scope: scope for scope in agent.scopes}
    flow = pb.AuthorizationCodeOAuthFlow(
        authorization_url=agent.authorization_url,
        token_url=agent.token_url,
        scopes=scopes,
        pkce_required=True,
    )
    scheme = pb.SecurityScheme(
        oauth2_security_scheme=pb.OAuth2SecurityScheme(
            description="Connection Hub: sign in to KDCube and consent",
            flows=pb.OAuthFlows(authorization_code=flow),
            oauth2_metadata_url=agent.oauth_metadata_url,
        )
    )
    card = pb.AgentCard(
        name=bounded_text(agent.name, 120),
        description=bounded_text(agent.description, 2000),
        version=bounded_text(agent.version, 40) or "1",
        supported_interfaces=[
            pb.AgentInterface(url=agent.base_url, protocol_binding="JSONRPC", protocol_version=PROTOCOL_VERSION),
            pb.AgentInterface(url=agent.base_url, protocol_binding="HTTP+JSON", protocol_version=PROTOCOL_VERSION),
        ],
        capabilities=pb.AgentCapabilities(streaming=False, push_notifications=False, extended_agent_card=False),
        security_schemes={SCHEME_NAME: scheme},
        security_requirements=[pb.SecurityRequirement(schemes={SCHEME_NAME: pb.StringList(list=list(agent.scopes))})],
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["text/plain", "application/json"],
        skills=[
            pb.AgentSkill(
                id=skill.skill_id,
                name=bounded_text(skill.name, 120),
                description=bounded_text(skill.description, 2000),
                tags=[bounded_text(tag, 40) for tag in skill.tags],
            )
            for skill in agent.skills
        ],
    )
    if agent.provider_organization:
        card.provider.CopyFrom(
            pb.AgentProvider(organization=bounded_text(agent.provider_organization, 120), url=agent.provider_url)
        )
    return MessageToDict(card)


def card_etag(card: dict[str, Any]) -> str:
    body = json.dumps(card, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return '"' + hashlib.sha256(body).hexdigest()[:32] + '"'


# --- the server --------------------------------------------------------------------------------

_REST_ROUTES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("POST", re.compile(r"message:send"), "on_message_send"),
    ("POST", re.compile(r"message:stream"), "on_message_send_stream"),
    ("GET", re.compile(r"tasks"), "list_tasks"),
    ("POST", re.compile(r"tasks/(?P<id>[^/:]+):cancel"), "on_cancel_task"),
    ("GET", re.compile(r"tasks/(?P<id>[^/:]+):subscribe"), "on_subscribe_to_task"),
    ("POST", re.compile(r"tasks/(?P<id>[^/:]+):subscribe"), "on_subscribe_to_task"),
    ("GET", re.compile(r"tasks/(?P<id>[^/:]+)"), "on_get_task"),
    ("GET", re.compile(r"tasks/(?P<id>[^/:]+)/pushNotificationConfigs"), "list_push_notifications"),
    ("POST", re.compile(r"tasks/(?P<id>[^/:]+)/pushNotificationConfigs"), "set_push_notification"),
)


def _origin_and_path(url: str) -> tuple[str, str]:
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError:
        return "", ""
    host = (parts.hostname or "").lower()
    port = parts.port if parts.port not in (None, 443, 80) else None
    netloc = f"{host}:{port}" if port else host
    return f"{parts.scheme.lower()}://{netloc}", parts.path.rstrip("/")


class A2AServer:
    def __init__(
        self,
        *,
        agent: PublishedAgent,
        target: A2ATarget,
        limiter: A2ALimiter | None = None,
        rate_limits: RateLimits = RateLimits(),
        part_limits: PartLimits = PartLimits(),
    ) -> None:
        self._agent = agent
        self._target = target
        self._limiter = limiter
        self._rate_limits = rate_limits
        self._part_limits = part_limits
        self._card = build_agent_card(agent)
        self._etag = card_etag(self._card)
        handler = _TargetRequestHandler(self)
        builder = _CallerContextBuilder()
        self._jsonrpc = JsonRpcDispatcher(request_handler=handler, context_builder=builder, enable_v0_3_compat=True)
        self._rest = RestDispatcher(request_handler=handler, context_builder=builder)

    @property
    def agent(self) -> PublishedAgent:
        return self._agent

    def card_response(self, request: Request) -> Response:
        headers = {"ETag": self._etag, "Cache-Control": "public, max-age=300"}
        if request.headers.get("if-none-match") == self._etag:
            return Response(status_code=304, headers=headers)
        return JSONResponse(self._card, headers=headers)

    async def handle(self, request: Request, *, caller: A2ACaller | None, path_tail: str) -> Response:
        """Serve one request under the agent's base URL; ``path_tail`` is the part after it."""

        tail = str(path_tail or "").strip("/")
        if tail in CARD_TAILS:
            if request.method in {"GET", "HEAD"}:
                return self.card_response(request)
            return _fixed(405, "a2a_not_found")
        if caller is None:
            return _fixed(401, "a2a_unauthenticated")
        if not self._entry_matches(caller):
            return _fixed(403, "a2a_entry_mismatch")
        setattr(request.state, _CALLER_STATE, caller)
        if tail == "":
            if request.method != "POST":
                return _fixed(405, "a2a_not_found")
            return await self._handle_jsonrpc(request, caller)
        for method, pattern, name in _REST_ROUTES:
            match = pattern.fullmatch(tail)
            if match and request.method == method:
                request.scope["path_params"] = {**dict(request.scope.get("path_params") or {}), **match.groupdict()}
                if name in {"on_message_send", "on_message_send_stream"}:
                    retry = await self._admit(caller)
                    if retry:
                        return _rest_rate_limited(retry)
                return await getattr(self._rest, name)(request)
        return _fixed(404, "a2a_not_found")

    async def _handle_jsonrpc(self, request: Request, caller: A2ACaller) -> Response:
        try:
            payload = json.loads(await request.body() or b"null")
        except (ValueError, UnicodeDecodeError):
            payload = None
        if not isinstance(payload, dict):
            # One request per call: a batch would count as one against the rate limit.
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
            )
        if str(payload.get("method") or "") in _SEND_METHODS:
            retry = await self._admit(caller)
            if retry:
                return JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": payload.get("id"),
                        "error": {"code": -32603, "message": "Rate limited", "data": [_rate_limited_info(retry)]},
                    }
                )
        return await self._jsonrpc.handle_requests(request)

    def _entry_matches(self, caller: A2ACaller) -> bool:
        entry_origin, entry_path = _origin_and_path(caller.entry_resource)
        base_origin, base_path = _origin_and_path(self._agent.base_url)
        return bool(entry_origin) and entry_origin == base_origin and entry_path == base_path

    async def _admit(self, caller: A2ACaller) -> int:
        if self._limiter is None:
            return 0
        counters = (
            (f"client:{caller.card_access_id}", int(self._rate_limits.per_client_per_hour)),
            (f"agent:{self._agent.slug}", int(self._rate_limits.per_agent_per_hour)),
        )
        try:
            return max(0, int(await self._limiter.admit(counters, window_seconds=3600)))
        except Exception:  # noqa: BLE001 - an unreadable budget refuses, it never admits
            return 60

    # --- message conversion ---

    def inbound(self, message: pb.Message, caller: A2ACaller) -> InboundMessage:
        limits = self._part_limits
        if message.role == pb.ROLE_AGENT or not message.message_id:
            raise _part_refused()
        if not message.parts or len(message.parts) > limits.max_parts:
            raise _part_refused()
        parts: list[InboundPart] = []
        total = 0
        for part in message.parts:
            kind = part.WhichOneof("content")
            if kind == "text":
                if len(part.text) > limits.max_text_chars:
                    raise _part_refused()
                parts.append(InboundPart(kind="text", text=part.text))
            elif kind == "data":
                data = MessageToDict(part.data)
                size = len(json.dumps(data, separators=(",", ":")).encode("utf-8"))
                if size > limits.max_data_bytes:
                    raise _part_refused()
                total += size
                parts.append(InboundPart(kind="data", data=data))
            elif kind == "raw":
                size = len(part.raw)
                if size > limits.max_part_bytes:
                    raise _part_refused()
                total += size
                parts.append(
                    InboundPart(
                        kind="file",
                        raw=bytes(part.raw),
                        filename=bounded_text(part.filename, 200) or "attachment",
                        media_type=bounded_text(part.media_type, 100) or "application/octet-stream",
                    )
                )
            else:  # "url" (no server-side fetch in this version) or an empty part
                raise _part_refused()
            if total > limits.max_task_bytes:
                raise _part_refused()
        key = hashlib.sha256(
            "\n".join((self._agent.slug, caller.card_access_id, message.message_id)).encode("utf-8")
        ).hexdigest()
        return InboundMessage(
            message_id=message.message_id,
            idempotency_key=key,
            context_id=message.context_id,
            task_id=message.task_id,
            parts=tuple(parts),
        )


def task_proto(view: TaskView) -> pb.Task:
    status = pb.TaskStatus(state=pb.TaskState.Value(view.state))
    if view.status_text:
        status.message.CopyFrom(
            pb.Message(
                message_id=f"status-{view.task_id}",
                role=pb.ROLE_AGENT,
                task_id=view.task_id,
                context_id=view.context_id,
                parts=[pb.Part(text=view.status_text)],
            )
        )
    artifacts = []
    for artifact in view.artifacts:
        parts = []
        for part in artifact.parts:
            if part.raw:
                parts.append(pb.Part(raw=part.raw, filename=part.filename, media_type=part.media_type))
            else:
                parts.append(pb.Part(text=part.text))
        artifacts.append(pb.Artifact(artifact_id=artifact.artifact_id, name=artifact.name, parts=parts))
    return pb.Task(id=view.task_id, context_id=view.context_id, status=status, artifacts=artifacts)


# --- the SDK seam ------------------------------------------------------------------------------


class _CallerContextBuilder(ServerCallContextBuilder):
    def build(self, request: Request) -> ServerCallContext:
        context = DefaultServerCallContextBuilder().build(request)
        context.state[_CALLER_STATE] = getattr(request.state, _CALLER_STATE, None)
        return context


def _caller(context: ServerCallContext) -> A2ACaller:
    caller = context.state.get(_CALLER_STATE)
    if not isinstance(caller, A2ACaller):
        raise InternalError()
    return caller


def _part_refused() -> InvalidParamsError:
    return InvalidParamsError(message="Part refused", data={"reason": "a2a_part_refused"})


def _target_error(exc: Exception) -> Exception:
    code = getattr(exc, "code", "") if isinstance(exc, A2ATargetError) else ""
    if code == "task_not_found":
        return TaskNotFoundError()
    if code in {"task_not_cancelable", "task_closed"}:
        return TaskNotCancelableError()
    if code == "disabled":
        return UnsupportedOperationError(message="A2A is off", data={"reason": "a2a_disabled"})
    if code == "unpublished":
        return UnsupportedOperationError(message="Agent unpublished", data={"reason": "a2a_agent_unpublished"})
    return InternalError()


class _TargetRequestHandler(RequestHandler):
    def __init__(self, server: A2AServer) -> None:
        self._server = server

    async def _call(self, method, *args, **kwargs):
        try:
            return await method(*args, **kwargs)
        except (TaskNotFoundError, TaskNotCancelableError, InvalidParamsError, UnsupportedOperationError):
            raise
        except Exception as exc:  # noqa: BLE001 - only fixed codes leave
            raise _target_error(exc) from None

    async def on_message_send(self, params: pb.SendMessageRequest, context: ServerCallContext) -> pb.Task:
        caller = _caller(context)
        message = self._server.inbound(params.message, caller)
        target = self._server._target
        if message.task_id:
            view = await self._call(target.follow_up, caller, message.task_id, message)
        else:
            view = await self._call(target.submit, caller, message)
        return task_proto(view)

    async def on_get_task(self, params: pb.GetTaskRequest, context: ServerCallContext) -> pb.Task | None:
        view = await self._call(self._server._target.read, _caller(context), params.id)
        if view is None:
            raise TaskNotFoundError()
        return task_proto(view)

    async def on_cancel_task(self, params: pb.CancelTaskRequest, context: ServerCallContext) -> pb.Task | None:
        view = await self._call(self._server._target.cancel, _caller(context), params.id)
        if view is None:
            raise TaskNotFoundError()
        return task_proto(view)

    async def on_list_tasks(self, params: pb.ListTasksRequest, context: ServerCallContext) -> pb.ListTasksResponse:
        size = min(max(int(params.page_size or 50), 1), 100)
        page = await self._call(
            self._server._target.list,
            _caller(context),
            context_id=params.context_id,
            page_size=size,
            page_token=params.page_token,
        )
        tasks = [task_proto(view) for view in page.tasks[:size]]
        return pb.ListTasksResponse(
            tasks=tasks, next_page_token=page.next_page_token, page_size=len(tasks), total_size=len(tasks)
        )

    async def on_message_send_stream(
        self, params: pb.SendMessageRequest, context: ServerCallContext
    ) -> AsyncGenerator[Any]:
        raise UnsupportedOperationError()
        yield  # pragma: no cover - makes this an async generator

    async def on_subscribe_to_task(
        self, params: pb.SubscribeToTaskRequest, context: ServerCallContext
    ) -> AsyncGenerator[Any]:
        raise UnsupportedOperationError()
        yield  # pragma: no cover

    async def on_create_task_push_notification_config(self, params, context):
        raise PushNotificationNotSupportedError()

    async def on_get_task_push_notification_config(self, params, context):
        raise PushNotificationNotSupportedError()

    async def on_list_task_push_notification_configs(self, params, context):
        raise PushNotificationNotSupportedError()

    async def on_delete_task_push_notification_config(self, params, context):
        raise PushNotificationNotSupportedError()

    async def on_get_extended_agent_card(self, params, context):
        raise ExtendedAgentCardNotConfiguredError()


# --- fixed responses ---------------------------------------------------------------------------


def _fixed(status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status)


def _rate_limited_info(retry_after: int) -> dict[str, Any]:
    """A google.rpc.ErrorInfo, the shape the SDK gives every other A2A error's details."""
    return {
        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
        "reason": "RATE_LIMITED",
        "domain": "a2a-protocol.org",
        "metadata": {"reason": "a2a_rate_limited", "retry_after_seconds": str(int(retry_after))},
    }


def _rest_rate_limited(retry_after: int) -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "message": "Rate limited",
                "details": [_rate_limited_info(retry_after)],
            }
        },
        status_code=429,
        headers={"Retry-After": str(int(retry_after))},
    )


__all__ = [
    "A2ACaller",
    "A2ALimiter",
    "A2AServer",
    "A2ATarget",
    "A2ATargetError",
    "A2A_SERVER_ERROR_CODES",
    "CARD_TAILS",
    "InboundMessage",
    "InboundPart",
    "OutboundPart",
    "PartLimits",
    "PublishedAgent",
    "RateLimits",
    "TARGET_ERROR_CODES",
    "TaskArtifactView",
    "TaskPage",
    "TaskView",
    "build_agent_card",
    "card_etag",
    "task_proto",
]
