"""The per-credential lock's remaining proofs for the W464 eight-point gate (Infra, 2026-10-02).

1. Two operating-system processes holding one native credential take turns.
2. Same-credential races, each order forced by a held fake rather than
   sampled: a retire inside the refresh commit's window waits and leaves no
   credential (red on main 20e60a13: the CLI remove path left an orphan); a
   disconnect holding the lock first makes the refresh find no profile; a
   refresh that read before a disconnect re-puts nothing; a stale reconnect
   commit after a retire is refused with profile_not_found.
3. A bounded burst: sixteen profiles refreshing together at 0.25 s per keychain
   call complete without a lock timeout (red on main 20e60a13 with
   oauth_profile_lock_timeout). This is a regression test at that latency, not
   a guarantee under arbitrary host pressure.
4. The lock order holds statically: no credential lock or custody section is
   taken inside the store-wide lock, no slot lock inside a credential lock, and
   no direct keychain call (_in_custody or self._credentials) sits inside the
   store-wide lock. Calls reached through a helper are not followed.
"""

from __future__ import annotations

import ast
import asyncio
import subprocess
import sys
import threading
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


class _HeldRefreshOAuth(_OAuth):
    """The token endpoint call waits on a test gate, so a disconnect can run meanwhile."""

    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def refresh(self, **kwargs):
        self.entered.set()
        await self.release.wait()
        return await super().refresh(**kwargs)


@pytest.mark.asyncio
async def test_refresh_reads_then_disconnect_retires_then_the_refresh_commit_re_puts_nothing(tmp_path):
    """The interleave that left an orphan credential: refresh read the token, a
    disconnect retired the profile while the refresh was at the token endpoint,
    then the refresh tried to commit (Ops, PR 446)."""

    oauth = _HeldRefreshOAuth()
    service, profiles, credentials = await _authorized(tmp_path, ["agent-a"], oauth=oauth)
    profile = profiles.require("agent-a")
    refreshing = asyncio.create_task(service.refresh_access_token("agent-a"))
    await asyncio.wait_for(oauth.entered.wait(), 3)  # the refresh has read and is at the endpoint
    await service.revoke_and_retire(profile)  # the disconnect completes meanwhile
    assert profiles.get("agent-a") is None
    oauth.release.set()
    with pytest.raises(BaseException) as raised:
        await refreshing
    assert getattr(raised.value, "code", "") == "profile_not_found", raised.value
    assert credentials.get(profile.credential_ref) is None, "the late refresh commit re-put nothing"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["cli_remove", "disconnect"])
async def test_a_retire_inside_the_refresh_commit_window_waits_and_leaves_no_credential(tmp_path, path):
    """The orphan Ops found: the refresh commit has read the record and is
    writing the rotated token when a retire runs. Without the credential lock
    the retire removes both, then the commit's put re-creates the credential
    for a profile that no longer exists. The commit's put is held here, so the
    retire runs inside that window every time, not by chance."""

    service, profiles, credentials = await _authorized(tmp_path, ["agent-a"])
    profile = profiles.require("agent-a")
    order: list[str] = []
    original_put = credentials.put
    put_entered, put_release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()

    def held_put(ref, token):
        # Only the commit's put (the refreshed token) waits for the test. The
        # attempt record's put passes: the refresh releases the credential
        # lock before it calls the token endpoint, by design.
        if token.access_token == "refreshed-access":
            loop.call_soon_threadsafe(put_entered.set)
            assert put_release.wait(5)
            order.append("refresh-commit")
        return original_put(ref, token)

    credentials.put = held_put
    refreshing = asyncio.create_task(service.refresh_access_token("agent-a"))
    await asyncio.wait_for(put_entered.wait(), 3)  # the commit is inside its window
    if path == "cli_remove":  # ProfileService.remove: the sync retire, off the loop
        retiring = asyncio.create_task(asyncio.to_thread(service.retire_local, profile))
    else:  # ProfileService.disconnect
        retiring = asyncio.create_task(service.revoke_and_retire(profile))
    await asyncio.sleep(0.3)
    assert not retiring.done(), "the retire waits for the commit's credential lock"
    put_release.set()
    await refreshing
    await retiring
    order.append("retire-done")
    assert order == ["refresh-commit", "retire-done"]
    assert profiles.get("agent-a") is None
    assert credentials.get(profile.credential_ref) is None, "no orphan credential for a removed profile"


@pytest.mark.asyncio
async def test_a_disconnect_that_holds_the_lock_first_makes_the_refresh_find_no_profile(tmp_path):
    class HeldRevokeOAuth(_OAuth):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def revoke(self, **kwargs):
            self.entered.set()
            await self.release.wait()
            return await super().revoke(**kwargs)

    oauth = HeldRevokeOAuth()
    service, profiles, credentials = await _authorized(tmp_path, ["agent-a"], oauth=oauth)
    profile = profiles.require("agent-a")
    disconnecting = asyncio.create_task(service.revoke_and_retire(profile))
    await asyncio.wait_for(oauth.entered.wait(), 3)  # the disconnect holds the credential lock
    refreshing = asyncio.create_task(service.refresh_access_token("agent-a"))
    await asyncio.sleep(0.2)
    assert not refreshing.done(), "the refresh waits for the disconnect's credential lock"
    oauth.release.set()
    await disconnecting
    with pytest.raises(BaseException) as raised:
        await refreshing
    assert getattr(raised.value, "code", "") == "profile_not_found", raised.value
    assert profiles.get("agent-a") is None and credentials.get(profile.credential_ref) is None


@pytest.mark.asyncio
async def test_a_reconnect_commit_after_a_retire_refuses_and_restores_nothing(tmp_path):
    service, profiles, credentials = await _authorized(tmp_path, ["agent-a"])
    expected = profiles.require("agent-a")
    retiring = asyncio.to_thread(service.retire_local, expected)
    await retiring
    with pytest.raises(BaseException) as raised:
        await service._commit_reconnected_token(
            expected=expected,
            replacement=_token("reconnected-access", "reconnected-refresh"),
            replacement_metadata=expected.oauth,
        )
    assert getattr(raised.value, "code", "") == "profile_not_found", raised.value
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

    # Sixteen refreshes of about six custody calls each at 0.25 s is about
    # 24 s of keychain work on the one custody thread: well past the 10 s a
    # waiter gives the store-wide lock, which is what timed out at startup
    # when that lock was held across the keychain. 0.12 s sat at the boundary
    # and passed on the old code on one host (Ops, 2026-10-02).
    service._credentials = SlowKeychain(credentials, 0.25)
    started = time.monotonic()
    results = await asyncio.gather(
        *(service.refresh_access_token(name) for name in names), return_exceptions=True
    )
    failures = [r for r in results if isinstance(r, BaseException)]
    assert failures == [], [getattr(f, "code", type(f).__name__) for f in failures]
    assert time.monotonic() - started < 90


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
            if "_transaction" in taken or "_transaction" in held:
                for inner in ast.walk(ast.Module(body=node.body, type_ignores=[])):
                    if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                        called = inner.func
                        if called.attr == "_in_custody" or (
                            isinstance(called.value, ast.Attribute) and called.value.attr == "_credentials"
                        ):
                            violations.append(f"line {inner.lineno}: keychain call inside _transaction")
            for child in node.body:
                visit(child, held + taken)
            return
        for child in ast.iter_child_nodes(node):
            visit(child, held)

    visit(ast.parse(source), [])
    assert violations == [], violations
