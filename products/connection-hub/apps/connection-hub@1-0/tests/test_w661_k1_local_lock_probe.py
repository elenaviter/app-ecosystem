"""W661 K1 re-run probe (phase-1 lock): the Hub lock on a container-local root, Card files on the share.

It must PASS in role proc, be refused outside it (fail closed), and the no-lock control must FAIL.
"""

import os
import pathlib
import subprocess
import sys

PROBE = pathlib.Path(__file__).with_name("w661_k1_local_lock_probe.py")


def _run(share, locks, role, *extra):
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("GATEWAY_COMPONENT", None)
    if role:
        env["GATEWAY_COMPONENT"] = role
    return subprocess.run([sys.executable, str(PROBE), "run", "--dir", str(share), "--lock-root", str(locks),
                           "--iterations", "100", *extra], capture_output=True, text=True, timeout=300, env=env)


def test_the_phase_1_lock_passes_in_proc_refuses_elsewhere_and_the_control_shows_the_race(tmp_path):
    share, locks = tmp_path / "share", tmp_path / "locks"
    share.mkdir()
    ok = _run(share, locks, "proc")
    assert ok.returncode == 0 and ok.stdout.strip().splitlines()[-1].startswith(
        "K1 PASS lock=hub-local counter=200 expected=200 overlaps=0")
    refused = _run(share, locks, "ingress")
    assert refused.returncode == 1 and "card_store_write_wrong_process_role" in refused.stderr
    control = _run(share, locks, "proc", "--no-lock")
    assert control.returncode == 1 and control.stdout.strip().splitlines()[-1].startswith("K1 FAIL lock=off")
    assert list(share.iterdir()) == [] and (not locks.exists() or list(locks.iterdir()) == [])  # cleaned up
