"""A failed profile commit revokes its unrecorded grant, with or without cancellation (W461; probe by codex-infra)."""
import asyncio
import threading
import time

import pytest
from test_oauth_profiles import _service


@pytest.mark.parametrize("cancel", [False, True])
def test_failed_profile_commit_revokes_its_unrecorded_grant(tmp_path, cancel):
    service, profiles, credentials = _service(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original_put = credentials.put

    def held_put(credential_ref, token):
        if token.access_id == "access-agent":
            entered.set()
            assert release.wait(3), "test-only write gate timed out"
        original_put(credential_ref, token)

    def reject_profile(profile):
        raise OSError("synthetic profile commit failure")

    credentials.put = held_put
    profiles.add = reject_profile

    async def scenario():
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(service.authorize(name="agent", endpoint="https://hub.example.test/mcp"))

        def finish_write():
            if entered.wait(2) and cancel:
                loop.call_soon_threadsafe(task.cancel)
                time.sleep(0.05)
            release.set()

        finisher = threading.Thread(target=finish_write, daemon=True)
        finisher.start()
        try:
            await task
        except (asyncio.CancelledError, OSError):
            pass
        finally:
            release.set()
            await asyncio.to_thread(finisher.join, 2)

    asyncio.run(scenario())
    assert entered.is_set()
    assert profiles.get("agent") is None
    assert credentials.values == {}, "local rollback is complete"
    assert service._oauth.events == ["server.revoke"], (
        "the failed unrecorded grant was not revoked when cancellation coincided with commit failure"
    )
