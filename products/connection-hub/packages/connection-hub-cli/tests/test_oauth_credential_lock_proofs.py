"""The per-credential lock's remaining proofs for the W464 eight-point gate (Infra, 2026-10-02).

1. Two operating-system processes holding one native credential take turns.
2. Same-credential races (refresh against disconnect, reconnect commit against
   retire) never leave a credential behind for a removed profile.
3. A bounded burst: sixteen profiles refreshing together at a fixed keychain
   latency complete without a lock timeout. This is a regression test at that
   latency, not a guarantee under arbitrary host pressure.
4. The lock order holds statically: no credential lock or custody section is
   taken inside the store-wide lock, and no slot lock inside a credential lock.
"""

from __future__ import annotations

import ast
import asyncio
import subprocess
import sys
import time
from pathlib import Path

import pytest

from connection_hub.caller.authorization import profile_session
from test_oauth_profiles import ENDPOINT, _OAuth, _service, _token


async def _authorized(tmp_path, names, *, oauth=None):
    service, profiles, credentials = _service(tmp_path, oauth=oauth)
    for name in names:
        await service.authorize(name=name, endpoint=ENDPOINT)
    return service, profiles, credentials


@pytest.mark.asyncio
async def test_a_second_os_process_holding_the_credential_lock_makes_a_read_wait(tmp_path):
    service, profiles, _ = await _authorized(tmp_path, ["agent-a"])
    lock_path = service._credential_lock_path(profiles.require("agent-a").credential_ref)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time\n"
            "from filelock import FileLock\n"
            f"lock = FileLock({str(lock_path)!r})\n"
            "lock.acquire()\n"
            "print('held', flush=True)\n"
            "time.sleep(0.8)\n"
            "lock.release()\n",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert (await asyncio.to_thread(holder.stdout.readline)).strip() == "held"
        started = time.monotonic()
        assert await service.access_token("agent-a")
        assert time.monotonic() - started >= 0.5, "the read waited for the other process's lock"
    finally:
        holder.wait(timeout=10)


@pytest.mark.asyncio
async def test_a_refresh_racing_a_disconnect_never_leaves_an_orphan_credential(tmp_path):
    for round_number in range(8):
        root = tmp_path / f"round-{round_number}"
        root.mkdir()
        oauth = _OAuth()
        service, profiles, credentials = await _authorized(root, ["agent-a"], oauth=oauth)
        profile = profiles.require("agent-a")
        refreshing = asyncio.create_task(service.refresh_access_token("agent-a"))
        if round_number % 2:
            await asyncio.sleep(0)  # vary which side reaches the lock first
        disconnecting = asyncio.create_task(service.revoke_and_retire(profile))
        results = await asyncio.gather(refreshing, disconnecting, return_exceptions=True)
        assert profiles.get("agent-a") is None, results
        assert credentials.get(profile.credential_ref) is None, (
            f"round {round_number}: a credential outlived its removed profile: {results}"
        )


@pytest.mark.asyncio
async def test_a_reconnect_commit_after_a_retire_refuses_and_restores_nothing(tmp_path):
    service, profiles, credentials = await _authorized(tmp_path, ["agent-a"])
    expected = profiles.require("agent-a")
    retiring = asyncio.to_thread(service.retire_local, expected)
    await retiring
    with pytest.raises(Exception):
        await service._commit_reconnected_token(
            expected=expected,
            replacement=_token("reconnected-access", "reconnected-refresh"),
            replacement_metadata=expected.oauth,
        )
    assert profiles.get("agent-a") is None
    assert credentials.get(expected.credential_ref) is None, "a stale reconnect re-put nothing"


@pytest.mark.asyncio
async def test_sixteen_profiles_refreshing_together_finish_without_a_lock_timeout(tmp_path):
    names = [f"agent-{index:02d}" for index in range(16)]
    service, profiles, credentials = await _authorized(tmp_path, names)

    class SlowKeychain:
        """Every keychain call takes a fixed time, as on a loaded host."""

        def __init__(self, inner, seconds):
            self._inner, self._seconds = inner, seconds

        def get(self, ref):
            time.sleep(self._seconds)
            return self._inner.get(ref)

        def put(self, ref, token):
            time.sleep(self._seconds)
            return self._inner.put(ref, token)

        def remove(self, ref):
            time.sleep(self._seconds)
            return self._inner.remove(ref)

    # Sixteen refreshes of about six custody calls each at 0.12 s is about
    # 11.5 s of keychain work on the one custody thread: more than the 10 s a
    # waiter gives the store-wide lock, which is what timed out at startup
    # when that lock was held across the keychain.
    service._credentials = SlowKeychain(credentials, 0.12)
    started = time.monotonic()
    results = await asyncio.gather(
        *(service.refresh_access_token(name) for name in names), return_exceptions=True
    )
    failures = [r for r in results if isinstance(r, BaseException)]
    assert failures == [], [getattr(f, "code", type(f).__name__) for f in failures]
    assert time.monotonic() - started < 60


def _lock_kind(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        name = node.func.attr
        if name in ("_transaction", "_credential_lock", "_custody_section", "_refresh_slot", "_authorization_slot"):
            return name
    return None


def test_the_lock_order_holds_in_the_source():
    source = Path(profile_session.__file__).read_text()
    rank = {"_authorization_slot": 0, "_refresh_slot": 0, "_credential_lock": 1, "_custody_section": 1, "_transaction": 2}
    violations: list[str] = []

    def visit(node: ast.AST, held: list[str]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and held:
            return  # a nested function's body is not run under the outer lock
        if isinstance(node, ast.AsyncWith):
            taken = [kind for item in node.items if (kind := _lock_kind(item.context_expr))]
            for kind in taken:
                for outer in held:
                    if rank[kind] <= rank[outer] and not (kind == "_transaction" and outer == "_transaction"):
                        violations.append(f"line {node.lineno}: {kind} inside {outer}")
                    if kind == outer == "_transaction":
                        violations.append(f"line {node.lineno}: nested _transaction")
            for child in node.body:
                visit(child, held + taken)
            return
        for child in ast.iter_child_nodes(node):
            visit(child, held)

    visit(ast.parse(source), [])
    assert violations == [], violations
