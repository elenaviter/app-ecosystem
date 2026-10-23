# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""The inbound A2A server over an in-memory target, through a KDCube-shaped route.

The route mirrors a bundle's public ``@api`` path tail; the caller is what the
host's authentication would admit. Requests are the wire shapes outside A2A
clients send (1.0 with the version header, and 0.3 without it).
"""

from __future__ import annotations

import base64
import uuid

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("a2a.server.routes.jsonrpc_dispatcher")
fastapi = pytest.importorskip("fastapi")

from app_foundation.a2a.card import AgentSkill  # noqa: E402
from app_foundation.a2a.server import (  # noqa: E402
    A2ACaller,
    A2AServer,
    A2ATargetError,
    NoLimit,
    OutboundPart,
    PartLimits,
    PublishedAgent,
    RateLimits,
    TaskArtifactView,
    TaskPage,
    TaskView,
)

pytestmark = pytest.mark.asyncio

HOST = "https://hub.example"
PREFIX = "/api/integrations/bundles/t/p/problem-board@1-0/public/a2a"
BASE = f"{HOST}{PREFIX}/agent-x"
V1 = {"A2A-Version": "1.0"}

AGENT = PublishedAgent(
    slug="agent-x",
    name="Agent X",
    description="Reviews pull requests.",
    version="1",
    base_url=BASE,
    skills=(AgentSkill(skill_id="review", name="Review", description="Review a change", tags=("code",)),),
    authorization_url=f"{HOST}/oauth/authorize",
    token_url=f"{HOST}/oauth/token",
    scopes=("a2a:task",),
    oauth_metadata_url=f"{HOST}/.well-known/oauth-authorization-server",
)
ALICE = A2ACaller(principal="user:alice", client_label="Alice's agent", card_access_id="oauth-a", entry_resource=BASE)
BOB = A2ACaller(principal="user:bob", client_label="Bob's agent", card_access_id="oauth-b", entry_resource=BASE)


class Target:
    """Tasks owned by the Card that created them; idempotent by the server's key."""

    def __init__(self) -> None:
        self.tasks: dict[str, tuple[str, TaskView]] = {}
        self.by_key: dict[str, str] = {}
        self.messages: list = []
        self.raise_on_submit: Exception | None = None

    async def submit(self, caller, message):
        if self.raise_on_submit is not None:
            raise self.raise_on_submit
        self.messages.append(message)
        if message.idempotency_key in self.by_key:
            return self.tasks[self.by_key[message.idempotency_key]][1]
        task_id = f"task-{len(self.tasks) + 1}"
        view = TaskView(task_id=task_id, context_id=message.context_id or f"ctx-{task_id}", state="TASK_STATE_SUBMITTED")
        self.tasks[task_id] = (caller.card_access_id, view)
        self.by_key[message.idempotency_key] = task_id
        return view

    def _own(self, caller, task_id):
        owner, view = self.tasks.get(task_id, ("", None))
        return view if owner == caller.card_access_id else None

    async def follow_up(self, caller, task_id, message):
        view = self._own(caller, task_id)
        if view is None:
            raise A2ATargetError("task_not_found")
        self.messages.append(message)
        return view

    async def read(self, caller, task_id):
        return self._own(caller, task_id)

    async def cancel(self, caller, task_id):
        view = self._own(caller, task_id)
        if view is None:
            return None
        if view.state == "TASK_STATE_COMPLETED":
            raise A2ATargetError("task_not_cancelable")
        canceled = TaskView(task_id=view.task_id, context_id=view.context_id, state="TASK_STATE_CANCELED")
        self.tasks[task_id] = (caller.card_access_id, canceled)
        return canceled

    async def list(self, caller, *, context_id, page_size, page_token):
        views = [view for owner, view in self.tasks.values() if owner == caller.card_access_id]
        if context_id:
            views = [view for view in views if view.context_id == context_id]
        return TaskPage(tasks=tuple(views[:page_size]))


class Limiter:
    def __init__(self, retry: int = 0, fail: bool = False) -> None:
        self.retry = retry
        self.fail = fail
        self.calls: list = []

    async def admit(self, counters, *, window_seconds):
        self.calls.append((tuple(counters), window_seconds))
        if self.fail:
            raise RuntimeError("budget store down")
        return self.retry


def _app(server: A2AServer, caller: A2ACaller | None = ALICE):
    app = fastapi.FastAPI()
    current = {"caller": caller}

    @app.api_route(f"{PREFIX}/{{slug}}/{{path_tail:path}}", methods=["GET", "POST", "HEAD"])
    @app.api_route(f"{PREFIX}/{{slug}}", methods=["GET", "POST", "HEAD"])
    async def public(slug: str, request: fastapi.Request, path_tail: str = ""):
        return await server.handle(request, caller=current["caller"], path_tail=path_tail)

    return app, current


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=HOST)


def _server(target=None, **kwargs):
    kwargs.setdefault("limiter", NoLimit())
    return A2AServer(agent=AGENT, target=target or Target(), **kwargs)


def _reason(error):
    """Our fixed code, from the ErrorInfo detail the SDK attaches."""
    (info,) = error["data"] if "data" in error else error["details"]
    return info["metadata"]["reason"]


def _rpc(method, params):
    return {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}


def _message(*parts, message_id=None, task_id="", context_id="", role="ROLE_USER"):
    message = {"messageId": message_id or str(uuid.uuid4()), "role": role, "parts": list(parts) or [{"text": "hello"}]}
    if task_id:
        message["taskId"] = task_id
    if context_id:
        message["contextId"] = context_id
    return {"message": message}


# --- the Agent Card ---------------------------------------------------------------------------


async def test_the_card_is_public_with_an_etag_and_both_bindings() -> None:
    app, _ = _app(_server(), caller=None)
    async with _client(app) as client:
        response = await client.get(f"{PREFIX}/agent-x/agent-card.json")
        assert response.status_code == 200
        card = response.json()
        etag = response.headers["etag"]
        again = await client.get(f"{PREFIX}/agent-x/agent-card.json", headers={"If-None-Match": etag})
        alias = await client.get(f"{PREFIX}/agent-x/agent.json")
    assert again.status_code == 304
    assert alias.json() == card
    assert card["name"] == "Agent X" and card["skills"][0]["id"] == "review"
    bindings = {(item["protocolBinding"], item["url"], item["protocolVersion"]) for item in card["supportedInterfaces"]}
    assert bindings == {("JSONRPC", BASE, "1.0"), ("HTTP+JSON", BASE, "1.0")}
    assert not card["capabilities"].get("streaming") and not card["capabilities"].get("pushNotifications")
    flow = card["securitySchemes"]["connection_hub"]["oauth2SecurityScheme"]["flows"]["authorizationCode"]
    assert flow["pkceRequired"] is True and flow["tokenUrl"] == f"{HOST}/oauth/token"
    assert card["securityRequirements"] == [{"schemes": {"connection_hub": {"list": ["a2a:task"]}}}]


async def test_everything_but_the_card_needs_an_admitted_caller_at_this_entry() -> None:
    app, current = _app(_server(), caller=None)
    async with _client(app) as client:
        assert (await client.post(f"{PREFIX}/agent-x", json=_rpc("SendMessage", _message()), headers=V1)).status_code == 401
        current["caller"] = A2ACaller("user:alice", "x", "oauth-a", f"{HOST}{PREFIX}/agent-y")
        refused = await client.post(f"{PREFIX}/agent-x", json=_rpc("SendMessage", _message()), headers=V1)
    assert refused.status_code == 403 and refused.json() == {"error": "a2a_entry_mismatch"}


# --- JSON-RPC ---------------------------------------------------------------------------------


async def test_send_creates_a_task_with_converted_parts() -> None:
    target = Target()
    app, _ = _app(_server(target))
    raw = base64.b64encode(b"%PDF-1.4").decode()
    parts = [
        {"text": "Please review"},
        {"data": {"pr": 42}},
        {"raw": raw, "filename": "spec\u202e.pdf", "mediaType": "application/pdf"},
    ]
    async with _client(app) as client:
        response = await client.post(f"{PREFIX}/agent-x", json=_rpc("SendMessage", _message(*parts)), headers=V1)
    task = response.json()["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_SUBMITTED"
    (message,) = target.messages
    assert [part.kind for part in message.parts] == ["text", "data", "file"]
    assert message.parts[1].data == {"pr": 42}
    assert message.parts[2].raw == b"%PDF-1.4" and message.parts[2].filename == "spec .pdf"


async def test_the_idempotency_key_is_per_client_card() -> None:
    target = Target()
    app, current = _app(_server(target))
    async with _client(app) as client:
        first = await client.post(f"{PREFIX}/agent-x", json=_rpc("SendMessage", _message(message_id="m-1")), headers=V1)
        retry = await client.post(f"{PREFIX}/agent-x", json=_rpc("SendMessage", _message(message_id="m-1")), headers=V1)
        current["caller"] = BOB
        other = await client.post(f"{PREFIX}/agent-x", json=_rpc("SendMessage", _message(message_id="m-1")), headers=V1)
    ids = [response.json()["result"]["task"]["id"] for response in (first, retry, other)]
    assert ids[0] == ids[1] and ids[2] != ids[0]
    assert target.messages[0].idempotency_key != target.messages[2].idempotency_key


@pytest.mark.parametrize(
    "parts,role",
    [
        ([{"url": "https://files.example/x.pdf"}], "ROLE_USER"),
        ([{"raw": base64.b64encode(b"x" * 33).decode(), "filename": "big.bin"}], "ROLE_USER"),
        ([{"text": "x" * 11}], "ROLE_USER"),
        ([{"text": "a"}] * 4, "ROLE_USER"),
        ([{"text": "hello"}], "ROLE_AGENT"),
    ],
)
async def test_refused_parts_never_reach_the_target(parts, role) -> None:
    target = Target()
    limits = PartLimits(max_parts=3, max_text_chars=10, max_part_bytes=32, max_task_bytes=64)
    app, _ = _app(_server(target, part_limits=limits))
    async with _client(app) as client:
        response = await client.post(
            f"{PREFIX}/agent-x", json=_rpc("SendMessage", _message(*parts, role=role)), headers=V1
        )
    error = response.json()["error"]
    assert error["code"] == -32602 and _reason(error) == "a2a_part_refused"
    assert target.messages == []


async def test_another_cards_task_is_not_found() -> None:
    app, current = _app(_server())
    async with _client(app) as client:
        created = await client.post(f"{PREFIX}/agent-x", json=_rpc("SendMessage", _message()), headers=V1)
        task_id = created.json()["result"]["task"]["id"]
        own = await client.post(f"{PREFIX}/agent-x", json=_rpc("GetTask", {"id": task_id}), headers=V1)
        current["caller"] = BOB
        theirs = await client.post(f"{PREFIX}/agent-x", json=_rpc("GetTask", {"id": task_id}), headers=V1)
        cancel = await client.post(f"{PREFIX}/agent-x", json=_rpc("CancelTask", {"id": task_id}), headers=V1)
        listed = await client.post(f"{PREFIX}/agent-x", json=_rpc("ListTasks", {}), headers=V1)
        follow = await client.post(
            f"{PREFIX}/agent-x", json=_rpc("SendMessage", _message(task_id=task_id)), headers=V1
        )
    assert own.json()["result"]["id"] == task_id
    assert theirs.json()["error"]["code"] == -32001
    assert cancel.json()["error"]["code"] == -32001
    assert listed.json()["result"].get("tasks", []) == []
    assert follow.json()["error"]["code"] == -32001


async def test_cancel_list_and_follow_up() -> None:
    target = Target()
    app, _ = _app(_server(target))
    async with _client(app) as client:
        created = await client.post(f"{PREFIX}/agent-x", json=_rpc("SendMessage", _message()), headers=V1)
        task_id = created.json()["result"]["task"]["id"]
        follow = await client.post(
            f"{PREFIX}/agent-x", json=_rpc("SendMessage", _message({"text": "more"}, task_id=task_id)), headers=V1
        )
        canceled = await client.post(f"{PREFIX}/agent-x", json=_rpc("CancelTask", {"id": task_id}), headers=V1)
        listed = await client.post(f"{PREFIX}/agent-x", json=_rpc("ListTasks", {"pageSize": 500}), headers=V1)
    assert follow.json()["result"]["task"]["id"] == task_id and target.messages[1].task_id == task_id
    assert canceled.json()["result"]["status"]["state"] == "TASK_STATE_CANCELED"
    assert [task["id"] for task in listed.json()["result"]["tasks"]] == [task_id]


async def test_status_text_and_artifacts_are_what_the_target_wrote() -> None:
    target = Target()
    target.tasks["task-9"] = (
        "oauth-a",
        TaskView(
            task_id="task-9",
            context_id="ctx-9",
            state="TASK_STATE_COMPLETED",
            status_text="Done.",
            artifacts=(
                TaskArtifactView(
                    artifact_id="a-1",
                    name="reply",
                    parts=(OutboundPart(text="Looks good"), OutboundPart(raw=b"ok", filename="r.txt", media_type="text/plain")),
                ),
            ),
        ),
    )
    app, _ = _app(_server(target))
    async with _client(app) as client:
        task = (await client.post(f"{PREFIX}/agent-x", json=_rpc("GetTask", {"id": "task-9"}), headers=V1)).json()["result"]
        not_cancelable = await client.post(f"{PREFIX}/agent-x", json=_rpc("CancelTask", {"id": "task-9"}), headers=V1)
    assert task["status"]["message"]["parts"] == [{"text": "Done."}]
    assert task["artifacts"][0]["parts"][0] == {"text": "Looks good"}
    assert base64.b64decode(task["artifacts"][0]["parts"][1]["raw"]) == b"ok"
    assert not_cancelable.json()["error"]["code"] == -32002


async def test_unsupported_methods_answer_with_fixed_codes() -> None:
    app, _ = _app(_server())
    async with _client(app) as client:
        stream = await client.post(
            f"{PREFIX}/agent-x",
            json=_rpc("SendStreamingMessage", _message()),
            headers={**V1, "Accept": "text/event-stream"},
        )
        push = await client.post(
            f"{PREFIX}/agent-x",
            json=_rpc("CreateTaskPushNotificationConfig", {"taskId": "task-1", "url": "https://x.example/h"}),
            headers=V1,
        )
        batch = await client.post(f"{PREFIX}/agent-x", json=[_rpc("GetTask", {"id": "x"})], headers=V1)
    assert "-32004" in stream.text
    assert push.json()["error"]["code"] in {-32003, -32602}
    assert batch.json()["error"]["code"] == -32600


async def test_version_negotiation() -> None:
    app, _ = _app(_server())
    async with _client(app) as client:
        no_header = await client.post(f"{PREFIX}/agent-x", json=_rpc("SendMessage", _message()))
        legacy = await client.post(
            f"{PREFIX}/agent-x",
            json=_rpc(
                "message/send",
                {"message": {"messageId": "m-03", "role": "user", "kind": "message", "parts": [{"kind": "text", "text": "hi"}]}},
            ),
        )
    assert no_header.json()["error"]["code"] == -32009
    assert legacy.json()["result"]["status"]["state"] == "submitted"


async def test_a_target_failure_leaks_no_text() -> None:
    target = Target()
    target.raise_on_submit = RuntimeError("secret internal path /srv/board and a token abc123")
    app, _ = _app(_server(target))
    async with _client(app) as client:
        response = await client.post(f"{PREFIX}/agent-x", json=_rpc("SendMessage", _message()), headers=V1)
    assert response.json()["error"]["code"] == -32603
    assert "secret" not in response.text and "abc123" not in response.text


@pytest.mark.parametrize("planted", ["InvalidParamsError", "TaskNotFoundError", "UnsupportedOperationError"])
async def test_an_sdk_error_raised_by_the_target_leaves_no_text(planted) -> None:
    from a2a.utils import errors

    target = Target()
    target.raise_on_submit = getattr(errors, planted)(
        message="secret internal path /srv/board", data={"token": "abc123"}
    )
    app, _ = _app(_server(target))
    async with _client(app) as client:
        response = await client.post(f"{PREFIX}/agent-x", json=_rpc("SendMessage", _message()), headers=V1)
    assert response.json()["error"]["code"] == -32603
    assert "secret" not in response.text and "abc123" not in response.text


# --- rate limits ------------------------------------------------------------------------------


async def test_rate_limits_refuse_before_the_target_on_both_bindings() -> None:
    target = Target()
    limiter = Limiter(retry=120)
    app, _ = _app(_server(target, limiter=limiter, rate_limits=RateLimits(per_client_per_hour=5, per_agent_per_hour=50)))
    async with _client(app) as client:
        rpc = await client.post(f"{PREFIX}/agent-x", json=_rpc("SendMessage", _message()), headers=V1)
        rest = await client.post(f"{PREFIX}/agent-x/message:send", json=_message(), headers=V1)
        read = await client.post(f"{PREFIX}/agent-x", json=_rpc("GetTask", {"id": "x"}), headers=V1)
    error = rpc.json()["error"]
    assert error["code"] == -32603 and _reason(error) == "a2a_rate_limited"
    assert error["data"][0]["metadata"]["retry_after_seconds"] == "120"
    assert rest.status_code == 429 and rest.headers["retry-after"] == "120"
    assert _reason(rest.json()["error"]) == "a2a_rate_limited"
    assert read.json()["error"]["code"] == -32001  # reads are not counted
    assert target.messages == []
    assert limiter.calls[0] == ((("client:oauth-a", 5), ("agent:agent-x", 50)), 3600)


async def test_a_server_cannot_be_built_without_a_limiter() -> None:
    with pytest.raises(TypeError):
        A2AServer(agent=AGENT, target=Target())  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="a2a_limiter_required"):
        A2AServer(agent=AGENT, target=Target(), limiter=None)  # type: ignore[arg-type]
    assert await NoLimit().admit((("client:x", 1),), window_seconds=3600) == 0


async def test_an_unreadable_budget_refuses() -> None:
    target = Target()
    app, _ = _app(_server(target, limiter=Limiter(fail=True)))
    async with _client(app) as client:
        response = await client.post(f"{PREFIX}/agent-x", json=_rpc("SendMessage", _message()), headers=V1)
    assert _reason(response.json()["error"]) == "a2a_rate_limited" and target.messages == []


# --- HTTP+JSON --------------------------------------------------------------------------------


async def test_the_rest_binding_sends_reads_cancels_and_lists() -> None:
    app, _ = _app(_server())
    async with _client(app) as client:
        sent = await client.post(f"{PREFIX}/agent-x/message:send", json=_message(), headers=V1)
        task_id = sent.json()["task"]["id"]
        read = await client.get(f"{PREFIX}/agent-x/tasks/{task_id}", headers=V1)
        canceled = await client.post(f"{PREFIX}/agent-x/tasks/{task_id}:cancel", json={}, headers=V1)
        listed = await client.get(f"{PREFIX}/agent-x/tasks", headers=V1)
        missing = await client.get(f"{PREFIX}/agent-x/tasks/nope", headers=V1)
        unknown = await client.get(f"{PREFIX}/agent-x/elsewhere", headers=V1)
    assert read.json()["id"] == task_id
    assert canceled.json()["status"]["state"] == "TASK_STATE_CANCELED"
    assert [task["id"] for task in listed.json()["tasks"]] == [task_id]
    assert missing.status_code == 404
    assert unknown.status_code == 404 and unknown.json() == {"error": "a2a_not_found"}
