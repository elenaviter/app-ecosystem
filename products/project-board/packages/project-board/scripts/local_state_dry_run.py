#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Count what relay housekeeping would move or delete in one field. Writes nothing (W287).

Run it before a cleanup window, against the field root that holds
``.problem-board/``, with the interpreter that runs ``pb`` (so the receipt
classification is the relay's own) or any Python 3.10+ (then legacy receipts
are counted but not classified):

    python3 local_state_dry_run.py <field-root> [--now 2026-09-26T05:00:00Z] [--json]

It reads directory listings and file metadata. It opens a file only to
classify a legacy reconciliation receipt (empty receipts are deleted with
their outbox rows, informative ones are moved) and to read the state of a
flat outbox row. It never writes, renames or deletes, and takes no lock, so
counts taken while a relay runs are a snapshot.

The rules mirror ``local_state_maintenance.run_local_state_maintenance``:

- legacy flat stores are migrated (moved, or deleted for empty receipts);
- settled outbox rows, published receipts and events older than 30 days are
  removed by hour folder; refused receipts are kept;
- keyed flat stores are removed by file age (30 days, journal receipts 90).

Size bounds (records or bytes per agent) are reported where exceeded, not
simulated.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

DAYS = 30
JOURNAL_RECEIPT_DAYS = 90
OUTBOX_MAX_RECORDS, OUTBOX_MAX_BYTES = 100_000, 200 * 1024 * 1024
RECEIPT_MAX_RECORDS, RECEIPT_MAX_BYTES = 50_000, 50 * 1024 * 1024
EVENT_MAX_RECORDS, EVENT_MAX_BYTES = 50_000, 100 * 1024 * 1024
MAILBOX_SKIP = {"reconciliation-receipts", "ignored", "undeliverable"}

try:  # The relay's own classification, when run with pb's interpreter.
    from project_board.client.reconciliation_receipts import receipt_carries_information
    from project_board.contract.mailbox_reconciliation_contract import normalize_receipt
except ImportError:  # pragma: no cover - counted, not classified
    receipt_carries_information = normalize_receipt = None  # type: ignore[assignment]


def _json_files(directory: Path) -> Iterator[os.DirEntry]:
    if not directory.is_dir():
        return
    with os.scandir(directory) as entries:
        for entry in entries:
            if entry.name.endswith(".json") and entry.is_file(follow_symlinks=False):
                yield entry


def _read(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _numbered(parent: Path, width: int) -> list[Path]:
    if not parent.is_dir():
        return []
    return sorted(p for p in parent.iterdir() if p.is_dir() and len(p.name) == width and p.name.isdigit())


def _hours(agent_root: Path) -> Iterator[tuple[Path, datetime]]:
    for year in _numbered(agent_root, 4):
        for month in _numbered(year, 2):
            for day in _numbered(month, 2):
                for hour in _numbered(day, 2):
                    try:
                        start = datetime(int(year.name), int(month.name), int(day.name), int(hour.name), tzinfo=timezone.utc)
                    except ValueError:
                        continue
                    yield hour, start


def _agents(store_root: Path) -> list[Path]:
    if not store_root.is_dir():
        return []
    return sorted(p for p in store_root.iterdir() if p.is_dir() and not p.name.startswith("."))


def _partitioned(store_root: Path, cutoff: datetime, *, keep=lambda name: False, max_records=0, max_bytes=0) -> dict[str, dict[str, int]]:
    """Per agent: records held, expired (removed), kept although expired, and bound excess."""

    result: dict[str, dict[str, int]] = {}
    for agent in _agents(store_root):
        counts = defaultdict(int)
        for hour, start in _hours(agent):
            names = [e.name for e in _json_files(hour)]
            size = sum(os.stat(hour / n).st_size for n in names if (hour / n).exists())
            counts["records"] += len(names)
            counts["bytes"] += size
            if start + timedelta(hours=1) <= cutoff:
                kept = sum(1 for n in names if keep(n))
                if kept:
                    counts["expired_but_kept"] += kept
                else:
                    counts["remove"] += len(names)
                    counts["remove_bytes"] += size
        pending = agent / "pending"
        counts["pending"] = sum(1 for _ in _json_files(pending))
        left = counts["records"] - counts["remove"]
        left_bytes = counts["bytes"] - counts["remove_bytes"]
        if max_records and left > max_records:
            counts["over_record_bound"] = left - max_records
        if max_bytes and left_bytes > max_bytes:
            counts["over_byte_bound"] = left_bytes - max_bytes
        if any(counts.values()):
            result[agent.name] = dict(counts)
    return result


def _legacy_receipts(project: Path, named: set[str]) -> dict[str, int]:
    """Classify legacy receipts; ``named`` collects the outbox ids deleted with empty ones."""

    counts = defaultdict(int)
    for directory in (project / "mail" / "reconciliation-receipts", project / "mail-reconciliation" / ".legacy-claimed"):
        for entry in _json_files(directory):
            counts["files"] += 1
            if normalize_receipt is None:
                counts["unclassified"] += 1
                continue
            record = _read(entry.path)
            try:
                receipt = normalize_receipt(record.get("receipt") or {})
            except Exception:  # noqa: BLE001 - any failure is "unreadable", as in the relay
                receipt = {}
            if not record or not str(receipt.get("reporter_worker_name") or "").strip():
                counts["unreadable_quarantine"] += 1
            elif receipt_carries_information(receipt):
                counts["move"] += 1
            else:
                counts["delete"] += 1
                ids = [str(value) for value in (record.get("publication") or {}).get("outbox_ids") or [] if value]
                counts["outbox_rows_named"] += len(ids)
                named.update(ids)
    return dict(counts)


def _flat_outbox(control: Path, cutoff: float, named: set[str]) -> dict[str, dict[str, int]]:
    """Flat pre-2b rows: moved into the layout, or deleted with an empty legacy receipt."""

    result: dict[str, dict[str, int]] = {}
    for folder in ("pending", "leased", "sent", "refused"):
        counts = defaultdict(int)
        for entry in _json_files(control / "outbox" / folder):
            if entry.name[:-5] in named:
                # A leased row defers its receipt instead; it moves once settled.
                counts["delete_with_empty_receipt" if folder != "leased" else "defers_its_receipt"] += 1
                continue
            counts["move"] += 1
            if folder in ("sent", "refused"):
                row = _read(entry.path)
                state = str(row.get("state") or "")
                counts[f"state_{state or 'unknown'}"] += 1
                if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                    counts["older_than_retention"] += 1
                if not row.get("outbox_id"):
                    counts["unreadable_quarantine"] += 1
        if counts:
            result[folder] = dict(counts)
    return result


def _keyed(control: Path, now: datetime) -> dict[str, dict[str, int]]:
    stores: list[tuple[str, str, Path, int]] = []
    for worker in sorted((control / "workers").glob("*")) if (control / "workers").is_dir() else ():
        if worker.is_dir():
            for name, sub in (("handled", "handled"), ("idempotency-mail", "idempotency/mail"), ("idempotency-events", "idempotency/events"), ("mail-processed", "mail/processed"), ("mail-by-control", "mail/by-control")):
                stores.append((name, worker.name, worker / sub, DAYS))
    for worker in sorted((control / "operator-responses").glob("*")) if (control / "operator-responses").is_dir() else ():
        if worker.is_dir():
            stores.append(("operator-responses", worker.name, worker, DAYS))
    for project in sorted((control / "projects").glob("*")) if (control / "projects").is_dir() else ():
        if not (project / "project.json").is_file():
            continue
        for kind in ("mail", "assignment-report", "project-report"):
            stores.append((f"idempotency-{kind}", "-", project / "idempotency" / kind, DAYS))
        for mailbox in sorted((project / "mail").glob("*")) if (project / "mail").is_dir() else ():
            if mailbox.is_dir() and mailbox.name not in MAILBOX_SKIP:
                stores.append(("mail-processed", mailbox.name, mailbox / "processed", DAYS))
                stores.append(("mail-by-control", mailbox.name, mailbox / "by-control", DAYS))
        for address in sorted((project / "mail" / "undeliverable").glob("*")) if (project / "mail" / "undeliverable").is_dir() else ():
            if address.is_dir():
                stores.append(("mail-undeliverable", address.name, address, DAYS))
        stores.append(("scope-leases-settled", "-", project / "scope-leases" / "settled", DAYS))
        stores.append(("journal-receipts", "-", project / "journals", JOURNAL_RECEIPT_DAYS))
    result: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for store, _agent, directory, days in stores:
        cutoff = (now - timedelta(days=days)).timestamp()
        for entry in _json_files(directory):
            result[store]["files"] += 1
            if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                result[store]["remove"] += 1
    return {store: dict(values) for store, values in result.items()}


def _field_totals(root: Path) -> dict[str, int]:
    files = size = 0
    for directory, _dirs, names in os.walk(root):
        for name in names:
            try:
                size += os.lstat(os.path.join(directory, name)).st_size
                files += 1
            except FileNotFoundError:
                continue
    return {"files": files, "bytes": size}


def dry_run(field_root: Path, now: datetime) -> dict[str, Any]:
    control = field_root / ".problem-board"
    if not control.is_dir():
        raise SystemExit(f"{field_root} holds no .problem-board/; pass the field root")
    cutoff = now - timedelta(days=DAYS)
    named: set[str] = set()
    projects = [
        project for project in (sorted((control / "projects").glob("*")) if (control / "projects").is_dir() else ())
        if (project / "project.json").is_file()
    ]
    legacy = {project.name: _legacy_receipts(project, named) for project in projects}
    report: dict[str, Any] = {
        "field_root": str(field_root),
        "now": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "receipt_classification": "relay" if normalize_receipt is not None else "unavailable (run with pb's interpreter)",
        "field_totals": _field_totals(field_root),
        "flat_outbox": _flat_outbox(control, cutoff.timestamp(), named),
        "keyed_flat_stores": _keyed(control, now),
        "projects": {},
    }
    unscoped = control / "unscoped" / "outbox"
    if unscoped.is_dir():
        report["unscoped_outbox"] = _partitioned(unscoped, cutoff, max_records=OUTBOX_MAX_RECORDS, max_bytes=OUTBOX_MAX_BYTES)
    for project in projects:
        entry: dict[str, Any] = {
            "legacy_receipts": legacy[project.name],
            "legacy_flat_events": {"move": sum(1 for _ in _json_files(project / "events"))},
            "receipts": _partitioned(
                project / "mail-reconciliation", cutoff,
                keep=lambda name: len(name.split("_")) > 3 and name.split("_")[2] != "published",
                max_records=RECEIPT_MAX_RECORDS, max_bytes=RECEIPT_MAX_BYTES,
            ),
            "outbox": _partitioned(project / "outbox", cutoff, max_records=OUTBOX_MAX_RECORDS, max_bytes=OUTBOX_MAX_BYTES),
            "events": _partitioned(project / "events", cutoff, max_records=EVENT_MAX_RECORDS, max_bytes=EVENT_MAX_BYTES),
        }
        report["projects"][project.name] = entry
    return report


ACTIONS = ("move", "delete", "remove", "remove_bytes", "delete_with_empty_receipt", "defers_its_receipt", "unreadable_quarantine", "expired_but_kept", "over_record_bound", "over_byte_bound")


def totals(report: dict[str, Any]) -> dict[str, int]:
    """Every action summed over stores and agents: the numbers a window is decided on."""

    found: dict[str, int] = defaultdict(int)

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, inner in value.items():
                if key in ACTIONS and isinstance(inner, int):
                    found[key] += inner
                else:
                    walk(inner)

    walk({key: value for key, value in report.items() if key != "field_totals"})
    return {key: found.get(key, 0) for key in ACTIONS}


def _render(report: dict[str, Any], prefix: str = "") -> Iterator[str]:
    for key, value in report.items():
        if isinstance(value, dict):
            if value:
                yield from _render(value, f"{prefix}{key}.")
        else:
            yield f"{prefix}{key} = {value}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("field_root", type=Path, help="the directory that holds .problem-board/")
    parser.add_argument("--now", help="UTC time to evaluate retention at (default: now)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of key = value lines")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    if args.now:
        now = datetime.fromisoformat(args.now.replace("Z", "+00:00")).astimezone(timezone.utc)
    report = dry_run(args.field_root.expanduser(), now)
    report = {"totals": totals(report), **report}
    if args.json:
        json.dump(report, sys.stdout, indent=2, sort_keys=True)
        print()
    else:
        print("\n".join(_render(report)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
