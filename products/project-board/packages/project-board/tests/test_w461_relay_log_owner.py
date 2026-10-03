"""One thread owns the relay log file; nothing that logs ever waits on it (W461).

Before W461 every relay line was written by the thread that logged it, under
the rotating handler's lock, so a file system that hung held the relay's event
loop. Now a line is formatted and queued, and one owner thread writes and
rotates the file. What may wait is bounded; a dropped line is counted and the
count is written. A diagnostic line gets a receipt: appended, or failed with
its error kept for the caller to classify.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import textwrap
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from project_board.client.relay_logging import (
    RelayLogOwner,
    RelayQueueHandler,
    UtcPerLineFormatter,
    configure_relay_logging,
    relay_log_owner,
)
from project_board.client.relay_trace import DiagnosticWriter

SECRET = "sk-SENTINEL-owner-do-not-leak"


class _StuckHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.gate = threading.Event()
        self.threads: set[str] = set()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.threads.add(threading.current_thread().name)
        self.gate.wait(10)
        self.lines.append(record.getMessage())


def _logger(owner: RelayLogOwner, name: str) -> logging.Logger:
    log = logging.getLogger(name)
    log.handlers = [RelayQueueHandler(owner)]
    log.propagate = False
    log.setLevel(logging.INFO)
    return log


def test_logging_returns_at_once_while_the_file_handler_is_stuck() -> None:
    handler = _StuckHandler()
    owner = RelayLogOwner(handler)
    log = _logger(owner, "w461.stuck")
    log.warning("first")  # the owner takes it and hangs in the handler
    started = time.monotonic()
    for index in range(50):
        log.warning("line %d", index)
    assert time.monotonic() - started < 0.5, "a logging thread never waits on the file"
    handler.gate.set()
    assert owner.close(5.0)
    assert handler.lines[:2] == ["first", "line 0"] and len(handler.lines) == 51
    assert handler.threads == {"problem-board-relay-log"}, "only the owner touches the handler"


def test_lines_over_the_bound_are_dropped_counted_and_reported() -> None:
    handler = _StuckHandler()
    owner = RelayLogOwner(handler, max_records=5, max_bytes=1 << 20)
    log = _logger(owner, "w461.bounded")
    log.warning("hold")
    time.sleep(0.05)  # the owner holds "hold" in the handler
    for index in range(20):
        log.warning("line %d", index)
    assert owner.pending()[0] <= 5
    assert owner.dropped_records == 15
    handler.gate.set()
    assert owner.close(5.0)
    assert any("relay log dropped records=15" in line for line in handler.lines)


def test_a_diagnostic_receipt_succeeds_only_after_the_owner_wrote_and_flushed(tmp_path: Path) -> None:
    path = tmp_path / "relay.stderr.log"
    file_handler = RotatingFileHandler(path, maxBytes=10_000, backupCount=1, encoding="utf-8")
    file_handler.setFormatter(UtcPerLineFormatter("%(message)s"))
    owner = RelayLogOwner(file_handler)
    receipt = owner.submit_line("relay turn accounting v1 {}")
    receipt.result(5)
    assert "relay turn accounting v1 {}" in path.read_text(encoding="utf-8")
    assert owner.close(5.0)


def test_a_failed_write_flush_or_rollover_is_a_failed_receipt_without_its_text(tmp_path: Path) -> None:
    class _Broken(RotatingFileHandler):
        def __init__(self, path: Path, failure: str) -> None:
            self.failure = failure
            super().__init__(path, maxBytes=10, backupCount=1, encoding="utf-8", delay=True)

        def doRollover(self) -> None:
            if self.failure == "rollover":
                raise OSError(SECRET)
            super().doRollover()

        def _open(self):
            stream = super()._open()
            failure = self.failure

            class _Stream:
                def write(self, text):
                    if failure == "write":
                        raise OSError(SECRET)
                    return stream.write(text)

                def flush(self):
                    if failure == "flush":
                        raise OSError(SECRET)
                    return stream.flush()

                def close(self):
                    return stream.close()

                def __getattr__(self, name):
                    return getattr(stream, name)

            return _Stream()

    async def scenario(failure: str):
        owner = RelayLogOwner(_Broken(tmp_path / f"{failure}.log", failure))
        owner.handler.stream = None
        writer = DiagnosticWriter(submit=owner.submit_line, timeout_seconds=2.0)
        receipt = await writer.write(("bucket", 1), "x" * 40)
        owner.close(2.0)
        return receipt, writer

    for failure in ("rollover", "write", "flush"):
        receipt, writer = asyncio.run(scenario(failure))
        assert receipt == "failed", failure
        assert writer.failure_classes == {"OSError": 1}
        assert SECRET not in repr(writer.receipts) + repr(writer.failure_classes)


def test_a_line_the_owner_refuses_is_a_dropped_receipt() -> None:
    handler = _StuckHandler()
    owner = RelayLogOwner(handler, max_records=1, max_bytes=1 << 20)
    log = _logger(owner, "w461.refused")
    log.warning("hold")  # the owner hangs in the handler with it
    time.sleep(0.05)
    log.warning("queued")  # fills the one-record bound

    async def scenario():
        writer = DiagnosticWriter(submit=owner.submit_line, timeout_seconds=0.05)
        return await writer.write(("bucket", 2), "refused"), writer

    receipt, writer = asyncio.run(scenario())
    assert receipt == "dropped" and writer.receipts["dropped_by_owner"] == 1
    handler.gate.set()
    owner.close(5.0)


def test_a_stuck_owner_never_holds_process_exit(tmp_path: Path) -> None:
    script = textwrap.dedent(
        f"""
        import logging, threading
        from pathlib import Path
        from project_board.client.relay_logging import configure_relay_logging, relay_log_owner
        configure_relay_logging(Path({str(tmp_path / "relay.json")!r}), mirror_to_stderr=False)
        never = threading.Event()
        owner = relay_log_owner()
        owner.handler.emit = lambda record: never.wait()
        logging.getLogger("x").warning("stuck forever")
        logging.getLogger("x").warning("queued behind it")
        print("exiting")
        """
    )
    started = time.monotonic()
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0 and "exiting" in done.stdout
    assert time.monotonic() - started < 30


def test_configured_relay_logging_writes_through_the_owner(tmp_path: Path) -> None:
    script = textwrap.dedent(
        f"""
        import logging, threading
        from pathlib import Path
        from project_board.client.relay_logging import configure_relay_logging, relay_log_owner
        configure_relay_logging(Path({str(tmp_path / "relay.json")!r}), mirror_to_stderr=False)
        seen = []
        owner = relay_log_owner()
        original = owner.handler.emit
        def emit(record):
            seen.append(threading.current_thread().name)
            original(record)
        owner.handler.emit = emit
        logging.getLogger("x").warning("through the owner")
        logging.shutdown()
        print(sorted(set(seen)))
        """
    )
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30, check=True)
    assert "['problem-board-relay-log']" in done.stdout
    assert "through the owner" in (tmp_path / "logs" / "relay.stderr.log").read_text(encoding="utf-8")
