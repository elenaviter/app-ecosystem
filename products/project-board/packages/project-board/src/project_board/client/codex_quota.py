"""Bounded, non-model quota reads through the installed Codex App Server.

Only initialize, account/read and account/rateLimits/read are sent. No thread,
turn, login, credential-file or earned-reset operation belongs to this adapter.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import signal
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ..contract.errors import DomainError
from .limit_state import KIND_OK, KIND_RATE_LIMITED, KIND_OUT_OF_TOKENS, limit_state_from_codex
from .session_delivery import _codex_executable, _command_environment

SOURCE_CODEX_APP_SERVER = "codex-app-server"
QUOTA_REFRESH_SECONDS = 60
QUOTA_FRESH_SECONDS = 120
# Operator-selected compatibility policy until durable per-worker settings land.
# Explicit None/0 is disabled; -1 permits available paid credits.
DEFAULT_USE_CREDITS = -1


def _credits_available(value: Any) -> bool:
    if not isinstance(value, Mapping) or value.get("hasCredits") is not True:
        return False
    if value.get("unlimited") is True:
        return True
    balance = value.get("balance")
    if isinstance(balance, bool) or not isinstance(balance, (str, int, float)):
        return False
    if len(str(balance)) > 128:
        return False
    try:
        amount = Decimal(str(balance))
        return amount.is_finite() and amount > 0
    except InvalidOperation:
        return False


def with_credit_policy(observation: Mapping[str, Any], use_credits: Any) -> dict[str, Any]:
    """Apply spending consent to sanitized provider evidence, never to usage."""
    candidate = dict(observation)
    if type(use_credits) is not int or use_credits != -1:
        return candidate
    raw = observation.get("buckets")
    if not isinstance(raw, Mapping) or not raw:
        return candidate
    buckets = {}
    changed = False
    for name, state in raw.items():
        if not isinstance(state, Mapping):
            continue
        bucket = dict(state)
        if (bucket.get("kind") == KIND_RATE_LIMITED and bucket.get("credits_available") is True
            and bucket.get("reached") in {"rate_limit_reached", "primary", "secondary"}):
            bucket.update(kind=KIND_OK, capacity_source="paid_credits", resets_at="")
            changed = True
        buckets[name] = bucket
    if not changed:
        return candidate
    ordered = sorted(buckets.values(), key=lambda state: (
        state.get("kind") in (KIND_RATE_LIMITED, KIND_OUT_OF_TOKENS),
        state.get("kind") == KIND_OK, str(state.get("resets_at") or "")), reverse=True)
    if ordered:
        selected = ordered[0]
        candidate.update({key: selected[key] for key in (
            "kind", "windows", "reached", "resets_at", "limit_id", "credits_available", "capacity_source"
        ) if key in selected}, buckets=buckets)
    return candidate


def account_fingerprint(email: str) -> str:
    clean = str(email or "").strip().lower()
    return hashlib.sha256(clean.encode()).hexdigest() if clean else ""


def _unavailable(code: str = "work_codex_quota_unavailable") -> DomainError:
    # Never copy native stderr, response errors, account text or credentials.
    return DomainError(code, "A fresh account-bound native quota reading is unavailable.", status=409)


def quota_state(result: Mapping[str, Any], *, observed_at: str,
                runtime_session_id: str, fingerprint: str) -> dict[str, Any]:
    """Normalize all native buckets; one healthy bucket cannot erase another."""
    raw = result.get("rateLimitsByLimitId")
    buckets = dict(raw) if isinstance(raw, Mapping) else {}
    default = result.get("rateLimits")
    if isinstance(default, Mapping):
        buckets.setdefault(str(default.get("limitId") or "codex"), default)
    if len(buckets) > 16 or any(len(str(name)) > 64 for name in buckets):
        raise _unavailable()
    states = {}
    for name, value in buckets.items():
        if not isinstance(value, Mapping):
            continue
        normalized = {"plan_type": value.get("planType"),
                      "rate_limit_reached_type": value.get("rateLimitReachedType")}
        for window_name in ("primary", "secondary"):
            window = value.get(window_name)
            if not isinstance(window, Mapping):
                continue
            used = window.get("usedPercent")
            minutes = window.get("windowDurationMins")
            reset = window.get("resetsAt")
            if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used) or used < 0:
                used = None
            if (isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0
                or isinstance(reset, bool) or not isinstance(reset, (int, float))
                or not math.isfinite(reset) or reset <= 0):
                used = None
                minutes = None
                reset = None
            normalized[window_name] = {"used_percent": used,
                "window_minutes": minutes, "resets_at": reset}
        state = limit_state_from_codex(normalized, observed_at=observed_at)
        # Paid balance is sensitive and unnecessary for eligibility. Earned
        # rateLimitResetCredits are a different feature and are never read.
        state["credits_available"] = _credits_available(value.get("credits"))
        state["limit_id"] = str(name)
        states[str(name)] = state
    ordered = sorted(states.values(), key=lambda state: (
        state["kind"] in (KIND_RATE_LIMITED, KIND_OUT_OF_TOKENS),
        state["kind"] == KIND_OK, str(state.get("resets_at") or "")), reverse=True)
    chosen = dict(ordered[0]) if ordered else limit_state_from_codex(None, observed_at=observed_at)
    chosen.update(source=SOURCE_CODEX_APP_SERVER, observed_at=observed_at,
                  runtime_session_id=runtime_session_id, account_email_sha256=fingerprint,
                  account_bound=True, buckets=states)
    return chosen


async def read_codex_quota(*, expected_email: str, runtime_session_id: str,
                          executable: Path | None = None, timeout_seconds: float = 10) -> dict[str, Any]:
    """Read the same registered account, without spending a model turn.

    The normal native configuration is inherited; a PB Card profile is never
    a Codex -p argument. The account is checked both before and after the read.
    Only this adapter's temporary subprocess is stopped on exit or timeout.
    """
    expected = account_fingerprint(expected_email)
    if not expected or not runtime_session_id:
        raise _unavailable("work_codex_quota_account_missing")
    binary = executable or _codex_executable()
    if binary is None:
        raise _unavailable("work_codex_quota_command_missing")
    process = None

    async def exchange():
        nonlocal process
        process = await asyncio.create_subprocess_exec(str(binary), "app-server", "--stdio",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=_command_environment(binary),
            limit=256 * 1024, start_new_session=True)
        async def send(value):
            process.stdin.write((json.dumps(value) + "\n").encode())
            await process.stdin.drain()

        async def request(number, method, params=None):
            await send({"id": number, "method": method,
                        **({"params": params} if params is not None else {})})
            while True:
                line = await process.stdout.readline()
                if not line:
                    raise _unavailable()
                value = json.loads(line)
                if not isinstance(value, Mapping) or value.get("id") != number:
                    continue
                if "error" in value or not isinstance(value.get("result"), Mapping):
                    raise _unavailable()
                return value["result"]

        await request(1, "initialize", {"clientInfo": {"name": "problem-board-quota", "version": "1"},
                                       "capabilities": {"experimentalApi": True}})
        await send({"method": "initialized", "params": {}})
        before = (await request(2, "account/read", {"refreshToken": False})).get("account")
        if not isinstance(before, Mapping) or before.get("type") != "chatgpt" or account_fingerprint(before.get("email")) != expected:
            raise _unavailable("work_codex_quota_account_mismatch")
        limits = await request(3, "account/rateLimits/read")
        observed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        after = (await request(4, "account/read", {"refreshToken": False})).get("account")
        if not isinstance(after, Mapping) or after.get("type") != "chatgpt" or account_fingerprint(after.get("email")) != expected:
            raise _unavailable("work_codex_quota_account_mismatch")
        return quota_state(limits, observed_at=observed_at,
                           runtime_session_id=runtime_session_id, fingerprint=expected)

    try:
        return await asyncio.wait_for(exchange(), timeout=max(0.05, min(float(timeout_seconds), 15)))
    except asyncio.TimeoutError as exc:
        raise _unavailable("work_codex_quota_timeout") from exc
    except DomainError:
        raise
    except (OSError, ValueError, TypeError, OverflowError) as exc:
        raise _unavailable() from exc
    finally:
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
            # The npm shim may exit promptly while its native child remains.
            # This new process group belongs solely to the temporary reader;
            # stop the group even when the leader already exited, never the
            # operator's existing console or another session's process.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass
            finally:
                # A child can ignore TERM even though the shim has exited.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.returncode is None:
                await process.wait()


__all__ = ["read_codex_quota", "quota_state", "account_fingerprint"]
