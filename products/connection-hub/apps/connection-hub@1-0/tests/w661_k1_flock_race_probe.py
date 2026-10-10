"""W661 K1: does the SDK Card mutation lock exclude two processes on THIS share?

Contract v6.2 section 1: the Card store is sound only where `observed_file_lock` (fcntl.flock) is
honoured between the processes that write Cards. This probe races processes on a directory the caller
names (the live runtime's Card share) and reports whether any two ever held the lock at once.

Disposable by construction: it creates ONE fresh folder `w661-k1-probe-<random>` under the given
directory, touches only the three files inside it (`.mutation.lock`, `holder`, `counter`) and removes
the folder at the end, also on failure. It reads no Card, credential, relay or configuration.

Modes:
  init   --dir D                                             make the shared probe folder, print its path
  run    --dir D [--procs 2] [--iterations 300] [--no-lock]   spawn the workers here, check, clean up
  worker --probe P --iterations N [--no-lock]                 one racer (start one per container)
  check  --probe P --expected N                               verdict for worker mode, then clean up

Output, last line: `K1 PASS ...` or `K1 FAIL ...` (exit 0 / 1). `--no-lock` is the control run: it MUST
print FAIL (overlaps > 0), which shows the probe can see a race on this share at all.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import shutil
import subprocess
import sys
import time

_RESOURCE = "delegated-card:w661-k1-probe"


def _lock(path: pathlib.Path, use_lock: bool):
    if not use_lock:
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def nothing():
            yield
        return nothing()
    from kdcube_ai_app.storage.observed_file_locks import observed_file_lock_async

    return observed_file_lock_async(lock_path=path, resource_id=_RESOURCE, operation="delegated-card-mutation",
                                    wait_seconds=60)


def _write_durable(path: pathlib.Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)


async def _worker(probe: pathlib.Path, iterations: int, use_lock: bool) -> int:
    overlaps = 0
    for _ in range(iterations):
        async with _lock(probe / ".mutation.lock", use_lock):
            try:  # exclusive create: if another holder's file is there, two processes are inside at once
                fd = os.open(probe / "holder", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                mine = True
            except FileExistsError:
                overlaps += 1
                mine = False
            value = int((probe / "counter").read_text(encoding="utf-8") or "0")
            time.sleep(0.002)  # widen the read-modify-write window
            _write_durable(probe / "counter", f"{value + 1}\n")
            if mine:
                (probe / "holder").unlink(missing_ok=True)
    _write_durable(probe / f"overlaps.{os.getpid()}", f"{overlaps}\n")
    return overlaps


def _verdict(probe: pathlib.Path, expected: int, label: str) -> int:
    counter = int((probe / "counter").read_text(encoding="utf-8"))
    overlaps = sum(int(p.read_text(encoding="utf-8")) for p in probe.glob("overlaps.*"))
    ok = counter == expected and overlaps == 0
    print(f"K1 {'PASS' if ok else 'FAIL'} {label} counter={counter} expected={expected} overlaps={overlaps} "
          f"probe={probe.name}", flush=True)
    return 0 if ok else 1


def _new_probe(directory: pathlib.Path) -> pathlib.Path:
    probe = directory / f"w661-k1-probe-{os.urandom(6).hex()}"
    probe.mkdir()
    _write_durable(probe / "counter", "0\n")
    return probe


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("run", "worker", "check", "init"))
    parser.add_argument("--dir", type=pathlib.Path)
    parser.add_argument("--probe", type=pathlib.Path)
    parser.add_argument("--procs", type=int, default=2)
    parser.add_argument("--iterations", type=int, default=300)
    parser.add_argument("--expected", type=int)
    parser.add_argument("--no-lock", action="store_true")
    args = parser.parse_args(argv)
    use_lock = not args.no_lock
    if args.mode == "init":  # worker mode: make the shared probe folder once, print its path
        print(_new_probe(args.dir), flush=True)
        return 0
    if args.mode == "worker":
        asyncio.run(_worker(args.probe, args.iterations, use_lock))
        return 0
    if args.mode == "check":
        try:
            return _verdict(args.probe, args.expected, "lock=" + ("off" if args.no_lock else "sdk-flock"))
        finally:
            shutil.rmtree(args.probe, ignore_errors=True)
    probe = _new_probe(args.dir)
    try:
        children = [subprocess.Popen([sys.executable, __file__, "worker", "--probe", str(probe),
                                      "--iterations", str(args.iterations)] + (["--no-lock"] if args.no_lock else []))
                    for _ in range(args.procs)]
        codes = [child.wait() for child in children]
        if any(codes):
            print(f"K1 FAIL worker-exit codes={codes}", flush=True)
            return 1
        return _verdict(probe, args.procs * args.iterations, "lock=" + ("off" if args.no_lock else "sdk-flock"))
    finally:
        shutil.rmtree(probe, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
