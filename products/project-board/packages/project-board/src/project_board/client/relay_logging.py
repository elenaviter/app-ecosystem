"""Bounded file logging for the supervised Problem Board relay."""

from __future__ import annotations

import concurrent.futures
import logging
import os
import queue
import sys
import threading
from datetime import datetime, timezone
from logging.handlers import QueueHandler, RotatingFileHandler
from pathlib import Path


RELAY_LOG_FILENAME = "relay.stderr.log"
# One active file plus three backups bounds each host target at 40 MiB while
# retaining several recent relay windows for diagnosis.
RELAY_LOG_MAX_BYTES = 10 * 1024 * 1024
RELAY_LOG_BACKUP_COUNT = 3
RELAY_CRASH_LOG_FILENAME = "relay.crash.log"
RELAY_CRASH_LOG_MAX_BYTES = 1024 * 1024
RELAY_LOGGER_NAME = "project_board.relay"


class UtcPerLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        timestamp = (
            datetime.fromtimestamp(record.created, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        prefix = f"{timestamp} {record.levelname} {record.name}: "
        return "\n".join(
            f"{prefix}{line}" for line in (rendered.splitlines() or [""])
        )


def relay_log_path(config_path: str | Path) -> Path:
    return Path(config_path).expanduser().resolve().parent / "logs" / RELAY_LOG_FILENAME


def relay_crash_log_path(config_path: str | Path) -> Path:
    return (
        Path(config_path).expanduser().resolve().parent
        / "logs"
        / RELAY_CRASH_LOG_FILENAME
    )


def _backup_path(path: Path, index: int) -> Path:
    return path.with_name(f"{path.name}.{index}")


def _trim_to_tail(path: Path, max_bytes: int) -> None:
    if path.stat().st_size <= max_bytes:
        return
    # Preserve the inode: the supervisor has already opened relay.crash.log as
    # stderr when relay startup reaches this function.
    with path.open("r+b") as target:
        target.seek(-max_bytes, os.SEEK_END)
        tail = target.read(max_bytes)
        target.seek(0)
        target.write(tail)
        target.truncate()


def prepare_relay_crash_log(
    path: Path,
    *,
    max_bytes: int = RELAY_CRASH_LOG_MAX_BYTES,
) -> None:
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        _trim_to_tail(path, max_bytes)


def prepare_relay_log(
    path: Path,
    *,
    max_bytes: int = RELAY_LOG_MAX_BYTES,
    backup_count: int = RELAY_LOG_BACKUP_COUNT,
) -> None:
    """Bound legacy files before ``RotatingFileHandler`` opens the active log."""

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if backup_count < 0:
        raise ValueError("backup_count cannot be negative")

    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = f"{path.name}."
    for candidate in path.parent.glob(f"{path.name}.*"):
        suffix = candidate.name.removeprefix(prefix)
        if suffix.isdigit() and int(suffix) > backup_count:
            candidate.unlink(missing_ok=True)

    if path.is_file() and path.stat().st_size > max_bytes:
        if backup_count == 0:
            path.unlink()
        else:
            _backup_path(path, backup_count).unlink(missing_ok=True)
            for index in range(backup_count - 1, 0, -1):
                source = _backup_path(path, index)
                if source.exists():
                    os.replace(source, _backup_path(path, index + 1))
            os.replace(path, _backup_path(path, 1))

    for index in range(1, backup_count + 1):
        backup = _backup_path(path, index)
        if backup.is_file():
            _trim_to_tail(backup, max_bytes)


def rotating_relay_handler(
    path: Path,
    *,
    max_bytes: int = RELAY_LOG_MAX_BYTES,
    backup_count: int = RELAY_LOG_BACKUP_COUNT,
) -> RotatingFileHandler:
    prepare_relay_log(path, max_bytes=max_bytes, backup_count=backup_count)
    handler = RotatingFileHandler(
        path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(UtcPerLineFormatter("%(message)s"))
    return handler



# W461: one thread owns the relay log file. Every relay line, an ordinary
# warning included, is only formatted and queued by the thread that logs it;
# the owner alone writes and rotates the file. A file system that hangs then
# holds the owner, never the relay's event loop, and nothing else waits on
# the file handler's lock. What waits is bounded by count and bytes; past
# that a line is dropped, counted, and the owner writes how many when it can.
RELAY_LOG_PENDING_MAX_RECORDS = 10_000
RELAY_LOG_PENDING_MAX_BYTES = 4 * 1024 * 1024
RELAY_LOG_CLOSE_WAIT_SECONDS = 1.0
RELAY_DIAGNOSTIC_LOGGER_NAME = "project_board.relay.diagnostics"


class RelayLogOwner:
    """The single writer of the relay's rotating log file.

    Only this daemon thread touches the handler: it writes, rotates, reports
    drops and finally closes it. A line keeps its place in the count and
    byte bounds until its write has ended, so what waits plus what is being
    written never exceeds them. A failed write is counted by error class; the
    handler's ``handleError`` (which prints to stderr) is never used.
    """

    def __init__(
        self,
        handler: logging.Handler,
        *,
        max_records: int = RELAY_LOG_PENDING_MAX_RECORDS,
        max_bytes: int = RELAY_LOG_PENDING_MAX_BYTES,
        thread_name: str = "problem-board-relay-log",
    ) -> None:
        self.handler = handler
        self.max_records = max(1, int(max_records))
        self.max_bytes = max(1, int(max_bytes))
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._pending_records = 0
        self._pending_bytes = 0
        self.dropped_records = 0
        self.dropped_bytes = 0
        self._reported_drops = (0, 0)
        # Lines the owner could not write, by error class; the owner keeps
        # running (a dead owner would lose every later relay line).
        self.owner_errors = 0
        self.owner_error_classes: dict[str, int] = {}
        self.enqueue_errors = 0
        self._closed = False
        _detach_from_logging_shutdown(handler)
        # A daemon: a write stuck in the file system never holds exit.
        self._thread = threading.Thread(target=self._run, name=thread_name, daemon=True)
        self._thread.start()

    @property
    def thread_ident(self) -> int | None:
        return self._thread.ident

    def pending(self) -> tuple[int, int]:
        """Lines waiting or being written, and their accounted bytes."""

        with self._lock:
            return self._pending_records, self._pending_bytes

    def _admit(self, size: int) -> bool:
        with self._lock:
            if (
                self._closed
                or self._pending_records + 1 > self.max_records
                or self._pending_bytes + size > self.max_bytes
            ):
                self.dropped_records += 1
                self.dropped_bytes += size
                return False
            self._pending_records += 1
            self._pending_bytes += size
            return True

    def submit_record(self, record: logging.LogRecord) -> bool:
        """Queue a prepared record; never blocks. False when it was dropped."""

        size = len(str(record.msg).encode("utf-8", "replace")) + 128
        if not self._admit(size):
            return False
        self._queue.put(("record", record, size, None))
        return True

    def submit_line(self, line: str) -> concurrent.futures.Future[None] | None:
        """Queue one diagnostic line with a receipt; None when it was dropped.

        The receipt succeeds only when the owner rolled over (if due), wrote
        and flushed the line without an exception: an append under the
        handler's file contract, not a durability guarantee. A failure keeps
        its exception for the caller to classify; nothing is printed.
        """

        size = len(line.encode("utf-8", "replace")) + 128
        if not self._admit(size):
            return None
        future: concurrent.futures.Future[None] = concurrent.futures.Future()
        self._queue.put(("line", line, size, future))
        return future

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._close_handler()
                return
            kind, payload, size, future = item
            try:
                if kind == "record":
                    self._emit_strict(payload)
                elif future.set_running_or_notify_cancel():
                    try:
                        self._emit_strict(self._diagnostic_record(payload))
                    except BaseException as exc:  # noqa: BLE001 - the receipt carries it
                        future.set_exception(exc)
                    else:
                        future.set_result(None)
            except Exception as exc:  # noqa: BLE001 - counted by class, never printed
                self._count_error(exc)
            finally:
                with self._lock:
                    self._pending_records -= 1
                    self._pending_bytes -= size
            try:
                self._report_drops()
            except Exception as exc:  # noqa: BLE001 - counted by class, never printed
                self._count_error(exc)

    def _count_error(self, exc: BaseException) -> None:
        name = type(exc).__name__
        if name not in {"OSError", "PermissionError", "ValueError", "RuntimeError", "UnicodeEncodeError"}:
            name = "other"
        with self._lock:
            self.owner_errors += 1
            self.owner_error_classes[name] = self.owner_error_classes.get(name, 0) + 1

    @staticmethod
    def _diagnostic_record(line: str) -> logging.LogRecord:
        return logging.LogRecord(
            RELAY_DIAGNOSTIC_LOGGER_NAME, logging.INFO, __file__, 0, line, None, None
        )

    def _emit_strict(self, record: logging.LogRecord) -> None:
        """What ``RotatingFileHandler.emit`` does, raising instead of ``handleError``."""

        handler = self.handler
        if not isinstance(handler, logging.StreamHandler):
            handler.emit(record)
            return
        handler.acquire()
        try:
            if isinstance(handler, RotatingFileHandler) and handler.shouldRollover(record):
                handler.doRollover()
            if handler.stream is None and isinstance(handler, logging.FileHandler):
                handler.stream = handler._open()
            if handler.stream is None:
                raise RuntimeError("relay_log_not_open")
            handler.stream.write(handler.format(record) + handler.terminator)
            handler.stream.flush()
        finally:
            handler.release()

    def _report_drops(self) -> None:
        with self._lock:
            drops = (self.dropped_records, self.dropped_bytes)
            if drops == self._reported_drops:
                return
            self._reported_drops = drops
        self._emit_strict(
            logging.LogRecord(
                RELAY_LOGGER_NAME,
                logging.WARNING,
                __file__,
                0,
                "Problem Board relay log dropped records=%d bytes=%d (totals since start)",
                drops,
                None,
            )
        )

    def _close_handler(self) -> None:
        try:
            self.handler.close()
        except Exception as exc:  # noqa: BLE001 - a failed final flush is counted, never raised
            self._count_error(exc)

    def close(self, wait_seconds: float = RELAY_LOG_CLOSE_WAIT_SECONDS) -> bool:
        """Stop taking lines and let the owner write the rest and close the file.

        The caller waits at most ``wait_seconds``; the owner itself closes the
        handler, so no file I/O or handler lock is taken by the caller. True
        when the owner has ended.
        """

        with self._lock:
            already = self._closed
            self._closed = True
        if not already:
            self._queue.put(None)
        self._thread.join(max(0.0, float(wait_seconds)))
        return not self._thread.is_alive()


class RelayQueueHandler(QueueHandler):
    """What the relay's loggers write to: format, queue, return (W461)."""

    def __init__(self, owner: RelayLogOwner) -> None:
        super().__init__(queue=None)  # type: ignore[arg-type]
        self.owner = owner

    def emit(self, record: logging.LogRecord) -> None:
        # QueueHandler.emit sends a formatting failure to handleError, which
        # prints a traceback to stderr on the logging thread. Here it is
        # counted instead, and the line is lost visibly (W461 review).
        try:
            self.owner.submit_record(self.prepare(record))
        except Exception:  # noqa: BLE001 - counted, never printed
            with self.owner._lock:
                self.owner.enqueue_errors += 1

    def enqueue(self, record: logging.LogRecord) -> None:
        self.owner.submit_record(record)

    def close(self) -> None:
        try:
            self.owner.close()
        finally:
            super().close()


def _detach_from_logging_shutdown(handler: logging.Handler) -> None:
    """Leave the owned handler to its owner alone, at exit too.

    ``logging.shutdown`` (run at interpreter exit) takes every handler's lock
    and closes it. The owner may be holding this handler's lock in a write the
    file system never finishes, and exit would then wait forever. The owner
    closes the handler itself once it has ended (``RelayLogOwner.close``).
    """

    references = getattr(logging, "_handlerList", None)
    if not isinstance(references, list):
        return
    for reference in list(references):
        try:
            if reference() is handler:
                references.remove(reference)
        except (TypeError, ValueError):
            continue


def relay_log_owner() -> RelayLogOwner | None:
    """The owner behind the relay's root log handler, when relay logging set one up."""

    for handler in logging.getLogger().handlers:
        if isinstance(handler, RelayQueueHandler):
            return handler.owner
    return None


def configure_relay_logging(
    config_path: Path | None,
    *,
    mirror_to_stderr: bool,
) -> Path | None:
    handlers: list[logging.Handler] = []
    path: Path | None = None
    if config_path is not None:
        path = relay_log_path(config_path)
        prepare_relay_crash_log(relay_crash_log_path(config_path))
        handlers.append(RelayQueueHandler(RelayLogOwner(rotating_relay_handler(path))))
    if mirror_to_stderr or not handlers:
        stream = logging.StreamHandler()
        stream.setFormatter(UtcPerLineFormatter("%(message)s"))
        handlers.append(stream)
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    install_relay_exception_hooks()
    return path



def _relay_sys_excepthook(exc_type, exc_value, exc_traceback) -> None:
    logging.getLogger(RELAY_LOGGER_NAME).critical(
        "Uncaught exception",
        exc_info=(exc_type, exc_value, exc_traceback),
    )
    sys.__excepthook__(exc_type, exc_value, exc_traceback)


def _relay_thread_excepthook(args: threading.ExceptHookArgs) -> None:
    thread_name = args.thread.name if args.thread is not None else "unknown"
    logging.getLogger(RELAY_LOGGER_NAME).critical(
        "Uncaught exception in thread %s",
        thread_name,
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )
    threading.__excepthook__(args)


def install_relay_exception_hooks() -> None:
    sys.excepthook = _relay_sys_excepthook
    threading.excepthook = _relay_thread_excepthook


def relay_log_status(
    path: Path,
    *,
    max_bytes: int = RELAY_LOG_MAX_BYTES,
    backup_count: int = RELAY_LOG_BACKUP_COUNT,
) -> dict[str, object]:
    files = []
    candidates = (
        path,
        *(_backup_path(path, i) for i in range(1, backup_count + 1)),
    )
    for candidate in candidates:
        if candidate.is_file():
            files.append(
                {"path": str(candidate), "size_bytes": candidate.stat().st_size}
            )
    crash_path = path.with_name(RELAY_CRASH_LOG_FILENAME)
    return {
        "path": str(path),
        "size_bytes": path.stat().st_size if path.is_file() else 0,
        "max_bytes": max_bytes,
        "backup_count": backup_count,
        "max_total_bytes": max_bytes * (backup_count + 1),
        "total_size_bytes": sum(int(item["size_bytes"]) for item in files),
        "files": files,
        "crash": {
            "path": str(crash_path),
            "size_bytes": crash_path.stat().st_size if crash_path.is_file() else 0,
            "max_bytes": RELAY_CRASH_LOG_MAX_BYTES,
        },
    }


__all__ = [
    "RELAY_LOG_BACKUP_COUNT",
    "RELAY_CRASH_LOG_FILENAME",
    "RELAY_CRASH_LOG_MAX_BYTES",
    "RELAY_LOG_FILENAME",
    "RELAY_LOG_MAX_BYTES",
    "UtcPerLineFormatter",
    "configure_relay_logging",
    "install_relay_exception_hooks",
    "prepare_relay_crash_log",
    "prepare_relay_log",
    "relay_crash_log_path",
    "relay_log_path",
    "relay_log_status",
    "rotating_relay_handler",
]
