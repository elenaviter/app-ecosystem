"""W661 K1: the disposable flock race probe itself (Root runs it on the live share; this proves the probe).

The SDK lock must PASS with zero overlaps; the no-lock control must FAIL, which shows the probe can see a race.
`W661_K1_PROBE_DIR` points the same run at another directory (for example a mounted share).
"""

import os
import pathlib
import subprocess
import sys

import pytest

PROBE = pathlib.Path(__file__).with_name("w661_k1_flock_race_probe.py")


def _run(directory, *extra):
    return subprocess.run([sys.executable, str(PROBE), "run", "--dir", str(directory), "--iterations", "100", *extra],
                          capture_output=True, text=True, timeout=300, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})


@pytest.mark.parametrize("use_lock", [True, False])
def test_the_sdk_flock_excludes_two_processes_and_the_control_shows_the_race(tmp_path, use_lock):
    directory = pathlib.Path(os.environ.get("W661_K1_PROBE_DIR") or tmp_path)
    result = _run(directory, *([] if use_lock else ["--no-lock"]))
    last = result.stdout.strip().splitlines()[-1]
    if use_lock:
        assert result.returncode == 0 and last.startswith("K1 PASS lock=sdk-flock counter=200 expected=200 overlaps=0")
    else:
        assert result.returncode == 1 and last.startswith("K1 FAIL lock=off")
    assert not [p for p in directory.iterdir() if p.name.startswith("w661-k1-probe-")]  # it cleaned up
