"""A small A2A 1.0 client over JSON-RPC: card fetch, send, get, cancel.

The caller owns credentials and authority. This client only guarantees the
transport boundary:

- every URL, the card URL and the card's JSON-RPC endpoint alike, passes the
  outbound endpoint policy, and every socket goes to an address the policy
  checked (``app_foundation.net``);
- caller headers (the credential) go only to the pinned card's JSON-RPC
  endpoint, never to the card URL; there are no redirects, no proxy variables
  and no cookies kept between calls;
- responses are read up to a byte limit;
- a failure is a fixed ``A2AClientError`` code. A remote error message, error
  ``data``, HTTP body or status text never becomes part of an error.

Task content (a status question, artifact text and data) is returned as
bounded data for the caller to treat as untrusted input.
"""

from __future__ import annotations

import dataclasses
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from app_foundation.a2a.card import PinnedAgentCard, bounded_text, card_digest, parse_agent_card
from app_foundation.a2a.errors import (
    A2A_CARD_INVALID,
    A2A_VERSION_UNSUPPORTED,
    REMOTE_AUTHORIZATION_REJECTED,
    REMOTE_ENDPOINT_DENIED,
    REMOTE_REQUEST_FAILED,
    REMOTE_RESPONSE_INVALID,
    REMOTE_RESPONSE_TOO_LARGE,
    REMOTE_TASK_NOT_CANCELABLE,
    REMOTE_TASK_NOT_FOUND,
    REMOTE_UNAVAILABLE,
    A2AClientError,
)
from app_foundation.net import OutboundEndpointDenied, OutboundEndpointPolicy, guarded_async_transport

PROTOCOL_VERSION = "1.0"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RESPONSE_BYTES = 1_048_576
MAX_ID_CHARS = 128
MAX_STATUS_TEXT_CHARS = 2000
MAX_ARTIFACTS = 16
MAX_PARTS_PER_ARTIFACT = 32
MAX_ARTIFACT_TEXT_CHARS = 16_384
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

TASK_STATES = frozenset(
    {
        "TASK_STATE_SUBMITTED",
        "TASK_STATE_WORKING",
        "TASK_STATE_INPUT_REQUIRED",
        "TASK_STATE_AUTH_REQUIRED",
        "TASK_STATE_COMPLETED",
        "TASK_STATE_FAILED",
        "TASK_STATE_CANCELED",
        "TASK_STATE_REJECTED",
    }
)
TERMINAL_TASK_STATES = frozenset(
    {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"}
)

# A2A JSON-RPC error codes that map to a specific fixed code; every other code,
# and every error message and data, collapses to REMOTE_REQUEST_FAILED.
_JSONRPC_ERROR_CODES = {
    -32001: REMOTE_TASK_NOT_FOUND,
    -32002: REMOTE_TASK_NOT_CANCELABLE,
    -32009: A2A_VERSION_UNSUPPORTED,
}


@dataclass(frozen=True, slots=True)
class ArtifactPart:
    text: str | None = None
    data: Any = None


@dataclass(frozen=True, slots=True)
class TaskArtifact:
    name: str
    parts: tuple[ArtifactPart, ...]


@dataclass(frozen=True, slots=True)
class A2ATask:
    task_id: str
    context_id: str
    state: str
    status_text: str
    artifacts: tuple[TaskArtifact, ...]
    dropped_parts: int = 0
    from_message: bool = False

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_TASK_STATES


def _bounded_id(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    if not text:
        return ""
    if not ID_PATTERN.fullmatch(text):
        raise A2AClientError(REMOTE_RESPONSE_INVALID)
    return text


def _parts(raw: Any) -> tuple[tuple[ArtifactPart, ...], int]:
    if not isinstance(raw, list):
        return (), 0
    parts: list[ArtifactPart] = []
    dropped = 0
    for part in raw:
        if len(parts) >= MAX_PARTS_PER_ARTIFACT or not isinstance(part, Mapping):
            dropped += 1
        elif isinstance(part.get("text"), str):
            parts.append(ArtifactPart(text=part["text"][:MAX_ARTIFACT_TEXT_CHARS]))
        elif "data" in part and isinstance(part.get("data"), (Mapping, list)):
            parts.append(ArtifactPart(data=part["data"]))
        else:
            # raw bytes, url and file parts are not passed on.
            dropped += 1
    return tuple(parts), dropped


def _status_text(status: Mapping[str, Any]) -> str:
    message = status.get("message")
    if not isinstance(message, Mapping):
        return ""
    parts = message.get("parts")
    if not isinstance(parts, list):
        return ""
    texts = [
        part["text"]
        for part in parts
        if isinstance(part, Mapping) and isinstance(part.get("text"), str)
    ]
    return bounded_text(" ".join(texts), MAX_STATUS_TEXT_CHARS)


_MALFORMED = (RecursionError, TypeError, ValueError, KeyError, AttributeError, IndexError)


def parse_task(raw: Any) -> A2ATask:
    """Validate a decoded Task. Any unexpected shape is REMOTE_RESPONSE_INVALID."""

    try:
        return _parse_task(raw)
    except A2AClientError:
        raise
    except _MALFORMED:
        raise A2AClientError(REMOTE_RESPONSE_INVALID) from None


def _parse_task(raw: Any) -> A2ATask:
    if not isinstance(raw, Mapping):
        raise A2AClientError(REMOTE_RESPONSE_INVALID)
    status = raw.get("status")
    if not isinstance(status, Mapping) or status.get("state") not in TASK_STATES:
        raise A2AClientError(REMOTE_RESPONSE_INVALID)
    task_id = _bounded_id(raw.get("id"))
    if not task_id:
        raise A2AClientError(REMOTE_RESPONSE_INVALID)
    artifacts: list[TaskArtifact] = []
    dropped = 0
    raw_artifacts = raw.get("artifacts")
    if raw_artifacts is None:
        raw_artifacts = []
    if not isinstance(raw_artifacts, list):
        raise A2AClientError(REMOTE_RESPONSE_INVALID)
    for entry in raw_artifacts[: MAX_ARTIFACTS + 1]:
        if len(artifacts) >= MAX_ARTIFACTS or not isinstance(entry, Mapping):
            dropped += 1
            continue
        parts, lost = _parts(entry.get("parts"))
        dropped += lost
        artifacts.append(TaskArtifact(name=bounded_text(entry.get("name"), 200), parts=parts))
    return A2ATask(
        task_id=task_id,
        context_id=_bounded_id(raw.get("contextId")),
        state=str(status["state"]),
        status_text=_status_text(status),
        artifacts=tuple(artifacts),
        dropped_parts=dropped,
    )


def _message_as_task(raw: Any) -> A2ATask:
    try:
        return _parse_message_as_task(raw)
    except A2AClientError:
        raise
    except _MALFORMED:
        raise A2AClientError(REMOTE_RESPONSE_INVALID) from None


def _parse_message_as_task(raw: Any) -> A2ATask:
    if not isinstance(raw, Mapping):
        raise A2AClientError(REMOTE_RESPONSE_INVALID)
    parts, dropped = _parts(raw.get("parts"))
    return A2ATask(
        task_id=_bounded_id(raw.get("taskId")),
        context_id=_bounded_id(raw.get("contextId")),
        state="TASK_STATE_COMPLETED",
        status_text="",
        artifacts=(TaskArtifact(name="reply", parts=parts),),
        dropped_parts=dropped,
        from_message=True,
    )


class A2AClient:
    def __init__(
        self,
        *,
        endpoint_policy: OutboundEndpointPolicy,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        transport: Any = None,
    ) -> None:
        self._policy = endpoint_policy
        self._timeout_seconds = float(timeout_seconds)
        self._max_response_bytes = int(max_response_bytes)
        self._transport = transport

    async def _checked_url(self, url: str) -> str:
        try:
            return await self._policy.validate(url)
        except OutboundEndpointDenied as exc:
            raise A2AClientError(REMOTE_ENDPOINT_DENIED, reason=exc.reason) from None

    async def _exchange(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        import httpx2

        transport = self._transport or guarded_async_transport(self._policy)
        timeout = httpx2.Timeout(self._timeout_seconds)
        try:
            async with httpx2.AsyncClient(
                transport=transport,
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream(
                    method,
                    url,
                    headers=dict(headers),
                    content=None if body is None else json.dumps(body).encode("utf-8"),
                ) as response:
                    status = response.status_code
                    if status in {401, 403}:
                        raise A2AClientError(REMOTE_AUTHORIZATION_REJECTED)
                    if 300 <= status < 400:
                        raise A2AClientError(REMOTE_RESPONSE_INVALID)
                    if status < 200 or status >= 300:
                        raise A2AClientError(REMOTE_UNAVAILABLE)
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > self._max_response_bytes:
                            raise A2AClientError(REMOTE_RESPONSE_TOO_LARGE)
                        chunks.append(chunk)
        except A2AClientError:
            raise
        except OutboundEndpointDenied as exc:
            raise A2AClientError(REMOTE_ENDPOINT_DENIED, reason=exc.reason) from None
        except (httpx2.HTTPError, OSError, RuntimeError):
            raise A2AClientError(REMOTE_UNAVAILABLE) from None
        try:
            return json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise A2AClientError(REMOTE_RESPONSE_INVALID) from None

    async def fetch_card(self, card_url: str) -> PinnedAgentCard:
        """Fetch, validate and pin a public Agent Card. No credential is sent."""

        url = await self._checked_url(card_url)
        raw = await self._exchange("GET", url, headers={"Accept": "application/json"})
        card = parse_agent_card(raw, card_url=url)
        endpoint = await self._checked_url(card.jsonrpc_url)
        if endpoint != card.jsonrpc_url:
            card = dataclasses.replace(card, jsonrpc_url=endpoint)
            card = dataclasses.replace(card, digest=card_digest(card.pinned_fields()))
        return card

    async def _call(
        self,
        card: PinnedAgentCard,
        method: str,
        params: Mapping[str, Any],
        *,
        headers: Mapping[str, str],
    ) -> Any:
        if not isinstance(card, PinnedAgentCard) or not card.jsonrpc_url:
            raise A2AClientError(A2A_CARD_INVALID)
        url = await self._checked_url(card.jsonrpc_url)
        if url != card.jsonrpc_url:
            # The pinned endpoint is the only place a credential may go.
            raise A2AClientError(A2A_CARD_INVALID)
        request_id = str(uuid.uuid4())
        sent_headers = {
            **{str(key): str(value) for key, value in dict(headers or {}).items()},
            "Content-Type": "application/json",
            "Accept": "application/json",
            "A2A-Version": PROTOCOL_VERSION,
        }
        reply = await self._exchange(
            "POST",
            url,
            headers=sent_headers,
            body={"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)},
        )
        if not isinstance(reply, Mapping) or reply.get("jsonrpc") != "2.0" or reply.get("id") != request_id:
            raise A2AClientError(REMOTE_RESPONSE_INVALID)
        error = reply.get("error")
        if error is not None:
            code = error.get("code") if isinstance(error, Mapping) else None
            raise A2AClientError(_JSONRPC_ERROR_CODES.get(code, REMOTE_REQUEST_FAILED))
        if "result" not in reply:
            raise A2AClientError(REMOTE_RESPONSE_INVALID)
        return reply["result"]

    async def send_message(
        self,
        card: PinnedAgentCard,
        *,
        headers: Mapping[str, str],
        text: str,
        data: Mapping[str, Any] | None = None,
        context_id: str = "",
        task_id: str = "",
        message_id: str = "",
    ) -> A2ATask:
        """Send one user message without waiting for the task to finish."""

        parts: list[dict[str, Any]] = [{"text": str(text)}]
        if data is not None:
            parts.append({"data": dict(data)})
        message: dict[str, Any] = {
            "messageId": message_id or str(uuid.uuid4()),
            "role": "ROLE_USER",
            "parts": parts,
        }
        if context_id:
            message["contextId"] = context_id
        if task_id:
            message["taskId"] = task_id
        result = await self._call(
            card,
            "SendMessage",
            {"message": message, "configuration": {"returnImmediately": True}},
            headers=headers,
        )
        if isinstance(result, Mapping) and "task" in result:
            return parse_task(result["task"])
        if isinstance(result, Mapping) and "message" in result:
            return _message_as_task(result["message"])
        raise A2AClientError(REMOTE_RESPONSE_INVALID)

    async def get_task(self, card: PinnedAgentCard, *, headers: Mapping[str, str], task_id: str) -> A2ATask:
        result = await self._call(card, "GetTask", {"id": _bounded_id(task_id)}, headers=headers)
        return parse_task(result)

    async def cancel_task(self, card: PinnedAgentCard, *, headers: Mapping[str, str], task_id: str) -> A2ATask:
        result = await self._call(card, "CancelTask", {"id": _bounded_id(task_id)}, headers=headers)
        return parse_task(result)


__all__ = [
    "A2AClient",
    "A2ATask",
    "ArtifactPart",
    "PROTOCOL_VERSION",
    "TASK_STATES",
    "TERMINAL_TASK_STATES",
    "TaskArtifact",
    "parse_task",
]
