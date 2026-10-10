"""W661 K1 option (a): two processes, the SDK flock on a container-local lock file, a shared counter.

The same check as the K1 probe (holder O_EXCL + read-modify-write counter), but every acquisition goes through
connection_hub's container_local_lock: the shared path is mapped to one local lock file.
"""

import os
import pathlib
import subprocess
import sys

_WORKER = r'''
import asyncio, os, pathlib, sys, time
from kdcube_ai_app.storage.observed_file_locks import observed_file_lock_async
from connection_hub.delegated_credentials.cards.locks import container_local_lock
share, root, n = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), int(sys.argv[3])
lock = container_local_lock(observed_file_lock_async, root)
async def main():
    overlaps = 0
    for _ in range(n):
        async with lock(lock_path=share / ".mutation.lock", resource_id="delegated-card:k1", operation="k1",
                        wait_seconds=60):
            try:
                os.close(os.open(share / "holder", os.O_CREAT | os.O_EXCL | os.O_WRONLY)); mine = True
            except FileExistsError:
                overlaps += 1; mine = False
            value = int((share / "counter").read_text())
            time.sleep(0.001)
            tmp = share / f".counter.{os.getpid()}"
            tmp.write_text(f"{value + 1}\n"); tmp.replace(share / "counter")
            if mine:
                (share / "holder").unlink()
    print(overlaps, flush=True)
asyncio.run(main())
'''


def test_two_processes_exclude_each_other_through_the_container_local_lock(tmp_path):
    share, root = tmp_path / "share", tmp_path / "local-locks"
    share.mkdir()
    (share / "counter").write_text("0\n")
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    workers = [subprocess.Popen([sys.executable, "-c", _WORKER, str(share), str(root), "150"], stdout=subprocess.PIPE,
                                text=True, env=env) for _ in range(2)]
    overlaps = [int(w.communicate(timeout=120)[0].strip()) for w in workers]
    assert all(w.returncode == 0 for w in workers)
    assert overlaps == [0, 0] and int((share / "counter").read_text()) == 300
    assert [p.name for p in root.iterdir()] and not (share / ".mutation.lock").exists()  # the lock lived locally
