from __future__ import annotations

import json
import re
import select
import subprocess
import time
from collections import deque
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO


PROBLEM_BOARD_WAKE_PREFIX = (
    "[Automatic Problem Board inbox wake; not written by the operator.]"
)
_WAKE_COMMAND = re.compile(
    r"\bpb worker receive --wake-id (?P<wake_id>[A-Za-z0-9_-]+)\b"
)


class CodexQueueProtocolError(RuntimeError):
    """The supported Codex queue protocol could not complete a request."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


class _CodexAppServerClient:
    """Small synchronous JSON-RPC client for queue inspection and deletion."""

    def __init__(
        self,
        executable: Path,
        *,
        environment: Mapping[str, str],
        timeout_seconds: float,
    ) -> None:
        self.executable = executable
        self.environment = dict(environment)
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.process: subprocess.Popen[str] | None = None
        self._next_request_id = 0
        self._stderr: deque[str] = deque(maxlen=20)

    def __enter__(self) -> _CodexAppServerClient:
        try:
            self.process = subprocess.Popen(
                [str(self.executable), "app-server", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=self.environment,
            )
        except OSError as exc:
            raise CodexQueueProtocolError(
                "codex_app_server_unreachable", type(exc).__name__
            ) from exc
        try:
            self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "problem-board-relay",
                        "version": "1",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
        except BaseException:
            self._close_process()
            raise
        return self

    def __exit__(self, *_exc: object) -> None:
        self._close_process()

    def _close_process(self) -> None:
        process = self.process
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        self.process = None

    def _process_detail(self) -> str:
        return "\n".join(self._stderr)[-2000:]

    def _read_response(self, request_id: int, *, deadline: float) -> dict[str, Any]:
        process = self.process
        if process is None or process.stdout is None or process.stderr is None:
            raise CodexQueueProtocolError("codex_app_server_not_started")
        streams: list[TextIO] = [process.stdout, process.stderr]
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexQueueProtocolError(
                    "codex_app_server_timeout", self._process_detail()
                )
            ready, _, _ = select.select(streams, [], [], remaining)
            if not ready:
                continue
            for stream in ready:
                line = stream.readline()
                if stream is process.stderr:
                    if line:
                        self._stderr.append(line.rstrip())
                    continue
                if not line:
                    raise CodexQueueProtocolError(
                        "codex_app_server_closed", self._process_detail()
                    )
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if payload.get("id") != request_id:
                    continue
                error = payload.get("error")
                if isinstance(error, Mapping):
                    raise CodexQueueProtocolError(
                        "codex_app_server_request_failed",
                        str(error.get("message") or error),
                    )
                result = payload.get("result")
                if not isinstance(result, Mapping):
                    raise CodexQueueProtocolError(
                        "codex_app_server_response_invalid", str(payload)[:2000]
                    )
                return dict(result)

    def request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        process = self.process
        if process is None or process.stdin is None:
            raise CodexQueueProtocolError("codex_app_server_not_started")
        self._next_request_id += 1
        request_id = self._next_request_id
        try:
            process.stdin.write(
                json.dumps(
                    {"id": request_id, "method": method, "params": dict(params)},
                    ensure_ascii=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise CodexQueueProtocolError(
                "codex_app_server_unreachable", type(exc).__name__
            ) from exc
        return self._read_response(
            request_id, deadline=time.monotonic() + self.timeout_seconds
        )


def _queued_wake_id(submission: Mapping[str, Any]) -> str:
    inputs = submission.get("input")
    if not isinstance(inputs, list):
        return ""
    text = "\n".join(
        str(value.get("text") or "")
        for value in inputs
        if isinstance(value, Mapping) and value.get("type") == "text"
    )
    if not text.startswith(PROBLEM_BOARD_WAKE_PREFIX):
        return ""
    match = _WAKE_COMMAND.search(text)
    return str(match.group("wake_id") if match else "")


def _list_queue(
    client: _CodexAppServerClient, *, thread_id: str
) -> list[dict[str, Any]]:
    submissions: list[dict[str, Any]] = []
    cursor = ""
    while True:
        params: dict[str, Any] = {"threadId": thread_id, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        page = client.request("thread/queue/list", params)
        submissions.extend(
            dict(value)
            for value in page.get("data") or []
            if isinstance(value, Mapping)
        )
        cursor = str(page.get("nextCursor") or "")
        if not cursor:
            return submissions


def reconcile_problem_board_wakes(
    executable: Path,
    *,
    environment: Mapping[str, str],
    thread_id: str,
    expected_wake_id: str = "",
    expected_submission_ids: Sequence[str] = (),
    timeout_seconds: float = 20,
) -> dict[str, Any]:
    """Make the native queue contain at most PB's one outstanding wake.

    Non-Problem-Board submissions are never touched. A queued PB wake with a
    different ID is stale, and a second submission for the current ID is a
    retry duplicate. Both are removed through Codex's own queue API.
    """

    clean_expected = str(expected_wake_id or "")
    known_expected = {
        str(value) for value in expected_submission_ids if str(value or "")
    }
    deleted: list[str] = []
    delete_races: list[str] = []
    stale_wake_ids: set[str] = set()
    with _CodexAppServerClient(
        executable,
        environment=environment,
        timeout_seconds=timeout_seconds,
    ) as client:
        before = _list_queue(client, thread_id=thread_id)
        kept_id = ""
        for submission in before:
            submission_id = str(submission.get("id") or "")
            wake_id = _queued_wake_id(submission)
            if not wake_id and submission_id in known_expected:
                wake_id = clean_expected
            if not wake_id:
                continue
            keep = bool(
                clean_expected and wake_id == clean_expected and not kept_id
            )
            if keep:
                kept_id = submission_id
                continue
            if wake_id != clean_expected:
                stale_wake_ids.add(wake_id)
            response = client.request(
                "thread/queue/delete",
                {
                    "threadId": thread_id,
                    "queuedSubmissionId": submission_id,
                },
            )
            if response.get("deleted") is True:
                deleted.append(submission_id)
            else:
                delete_races.append(submission_id)

        after = _list_queue(client, thread_id=thread_id)

    remaining_pb = [
        {
            "queued_submission_id": str(value.get("id") or ""),
            "wake_id": _queued_wake_id(value),
        }
        for value in after
        if _queued_wake_id(value)
    ]
    remaining_expected = [
        value["queued_submission_id"]
        for value in remaining_pb
        if value["wake_id"] == clean_expected
    ]
    stale_remaining = [
        value for value in remaining_pb if value["wake_id"] != clean_expected
    ]
    reconciled = not stale_remaining and len(remaining_expected) <= 1
    return {
        "adapter": "codex-queue",
        "state": "reconciled" if reconciled else "incomplete",
        "reconciled": reconciled,
        "expected_wake_id": clean_expected,
        "queued_submission_ids": remaining_expected,
        "deleted_submission_ids": deleted,
        "delete_race_submission_ids": delete_races,
        "already_absent_submission_ids": sorted(
            known_expected
            - {str(value.get("id") or "") for value in before}
        ),
        "stale_wake_ids": sorted(stale_wake_ids),
        "problem_board_queue_depth": len(remaining_pb),
        "queue_depth": len(after),
    }


__all__ = [
    "CodexQueueProtocolError",
    "PROBLEM_BOARD_WAKE_PREFIX",
    "reconcile_problem_board_wakes",
]
