"""Running journal entries: one per feature per UTC month, records appended (W1031 slice 1).

The operator, 25 Nov 2026: "correct is to simply add into the running jounral entry
per feature. per month, if in new month we do not have that featire jounral we add
it. so we do not have too much jounrals". This module holds the pure contracts the
later slices build on, and nothing that touches Git, the index or a receipt:

- the canonical identity of a month's entry, through the shared ``make_ref`` grammar,
  derived the same way by every author so two authors who start the same month write
  the same header;
- the record format: one section per record, opened by a machine-readable header
  comment, with a stable author, one work item, its own time and an optional
  correction target;
- the append-only rule a later version of a running entry must satisfy against the
  last accepted one.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from ..contract.errors import DomainError
from ..contract.refs import make_ref, parse_ref
from .io import canonical_json

RUNNING_SCHEMA = "pb.journal.running.v1"
RUNNING_KIND = "running"
FEATURES_DIRECTORY = "journal/features"
RECORD_MARKER = "pb-record"
_FEATURE_SEGMENT = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
_MAX_FEATURE_SEGMENTS = 3
_MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_SEMANTIC_MAX = 64
_MONTH_SUFFIX = len("-yyyy-mm")
_RECORD_HEADER = re.compile(r"^<!-- pb-record (\{.*\}) -->[ \t]*$", re.MULTILINE)
_RECORD_FIELDS = ("id", "recorded_at", "worker", "work_ref", "corrects")


def _refused(code: str, message: str, **details: Any) -> DomainError:
    return DomainError(code, message, status=400, details=details)


def normalize_feature(feature: Any) -> str:
    """One to three ``a-z0-9-`` segments separated by ``/``; never a path that escapes."""

    text = str(feature or "").strip().strip("/")
    segments = text.split("/") if text else []
    if (
        not segments
        or len(segments) > _MAX_FEATURE_SEGMENTS
        or any(_FEATURE_SEGMENT.fullmatch(segment) is None for segment in segments)
    ):
        raise _refused(
            "journal_feature_invalid",
            "A feature is one to three segments of a-z, 0-9 and '-', separated by '/'.",
            feature=str(feature or "")[:200],
        )
    return "/".join(segments)


def utc_instant(value: Any) -> datetime:
    """An aware UTC instant from an ISO 8601 string or datetime; naive input is refused."""

    if isinstance(value, datetime):
        instant = value
    else:
        text = str(value or "").strip()
        try:
            instant = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise _refused("journal_recorded_at_invalid", "recorded_at must be an ISO 8601 UTC time.",
                           recorded_at=text[:64]) from exc
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise _refused("journal_recorded_at_invalid", "recorded_at must carry a time zone (UTC).",
                       recorded_at=str(value)[:64])
    return instant.astimezone(timezone.utc)


def record_month(recorded_at: Any) -> str:
    """The UTC month (``YYYY-MM``) a record belongs to, from its own original time."""

    return utc_instant(recorded_at).strftime("%Y-%m")


def _month_start(month: str) -> datetime:
    match = _MONTH.fullmatch(str(month or ""))
    if match is None:
        raise _refused("journal_month_invalid", "A month is YYYY-MM.", month=str(month or "")[:16])
    return datetime(int(match.group(1)), int(match.group(2)), 1, tzinfo=timezone.utc)


def running_entry_key(*, project_ref: str, feature: str, month: str) -> str:
    """First 16 hex of SHA-256 over the canonical JSON of the entry's defining inputs."""

    _month_start(month)
    payload = {
        "schema": RUNNING_SCHEMA,
        "project_ref": str(project_ref or "").strip(),
        "feature": normalize_feature(feature),
        "month": month,
    }
    if not payload["project_ref"]:
        raise _refused("journal_project_ref_required", "A running entry belongs to one project.")
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:16]


def running_entry_semantic_name(*, feature: str, month: str) -> str:
    """``<feature slug>-<yyyy-mm>`` within 64 characters, the slug cut at a word before the month."""

    _month_start(month)
    words = [word for word in re.split(r"[^a-z0-9]+", normalize_feature(feature)) if word]
    budget = _SEMANTIC_MAX - _MONTH_SUFFIX
    selected: list[str] = []
    for word in words:
        candidate = "-".join((*selected, word))
        if len(candidate) > budget:
            break
        selected.append(word)
    slug = "-".join(selected) or "feature"
    return f"{slug}-{month}"


def running_entry_ref(*, project_ref: str, feature: str, month: str) -> str:
    """The canonical ``work:journal:...`` reference of one feature's entry for one month."""

    ref = make_ref(
        "journal",
        timestamp=_month_start(month),
        key=running_entry_key(project_ref=project_ref, feature=feature, month=month),
        semantic_name=running_entry_semantic_name(feature=feature, month=month),
    )
    parse_ref(ref)  # the shared grammar is the only grammar
    return ref


def running_entry_path(*, feature: str, month: str) -> str:
    """The entry's path relative to the journal home: ``journal/features/<feature>/<YYYY-MM>.md``."""

    _month_start(month)
    return f"{FEATURES_DIRECTORY}/{normalize_feature(feature)}/{month}.md"


@dataclass(frozen=True)
class JournalRecord:
    """One appended record of a running entry: header fields plus its exact section text."""

    id: str
    recorded_at: str
    worker: str
    work_ref: str
    corrects: str | None
    text: str

    def header(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "recorded_at": self.recorded_at,
            "worker": self.worker,
            "work_ref": self.work_ref,
            "corrects": self.corrects,
        }


def record_id(*, worker: str, recorded_at: Any, body: str) -> str:
    """``rec:<compact UTC time>:<first 12 hex of SHA-256 over (worker, recorded_at, body)>``."""

    instant = utc_instant(recorded_at)
    stamp = instant.strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha256(
        canonical_json({"worker": worker, "recorded_at": instant.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "body": body}).encode("utf-8")
    ).hexdigest()[:12]
    return f"rec:{stamp}:{digest}"


def format_record(*, worker: str, alias: str, work_ref: str, recorded_at: Any, title: str, body: str,
                  corrects: str | None = None) -> str:
    """The exact section text for one new record: header comment, readable heading, body."""

    worker = str(worker or "").strip()
    work_ref = str(work_ref or "").strip()
    if not worker:
        raise _refused("journal_record_worker_required", "A record names its stable worker.")
    if not work_ref:
        raise _refused("journal_record_work_ref_required", "A record names exactly one work item.")
    instant = utc_instant(recorded_at)
    clean_body = str(body or "").strip("\n")
    header = {
        "id": record_id(worker=worker, recorded_at=instant, body=clean_body),
        "recorded_at": instant.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "worker": worker,
        "work_ref": work_ref,
        "corrects": str(corrects).strip() if corrects else None,
    }
    heading = " · ".join(part for part in (header["recorded_at"], str(alias or "").strip(),
                                            str(title or "").strip()) if part)
    return (f"<!-- {RECORD_MARKER} {json.dumps(header, ensure_ascii=True, sort_keys=True)} -->\n"
            f"## {heading}\n\n{clean_body}\n")


def parse_records(text: str) -> list[JournalRecord]:
    """The records of a running entry, in file order. A malformed header refuses the whole text.

    A record runs from its header comment to the next header or the end of the file.
    The same id twice with different text is a conflict; the same id with the same
    text (a replayed append) is refused too, because a published file must hold each
    record once.
    """

    source = str(text or "")
    matches = list(_RECORD_HEADER.finditer(source))
    records: list[JournalRecord] = []
    seen: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        section = source[match.start():end]
        try:
            header = json.loads(match.group(1))
        except ValueError as exc:
            raise _refused("journal_record_malformed", "A record header is not valid JSON.",
                           offset=match.start()) from exc
        if not isinstance(header, dict) or set(header) != set(_RECORD_FIELDS):
            raise _refused("journal_record_malformed", "A record header must have exactly its fields.",
                           offset=match.start(), fields=sorted(header) if isinstance(header, dict) else None)
        if not all(isinstance(header[key], str) and header[key] for key in ("id", "recorded_at", "worker",
                                                                                "work_ref")):
            raise _refused("journal_record_malformed", "A record header field is empty.", offset=match.start())
        if header["corrects"] is not None and not (isinstance(header["corrects"], str) and header["corrects"]):
            raise _refused("journal_record_malformed", "corrects is a record id or null.", offset=match.start())
        utc_instant(header["recorded_at"])
        if header["id"] in seen:
            code = "journal_record_conflict" if seen[header["id"]] != section else "journal_record_duplicate"
            raise _refused(code, "A record id appears more than once.", record_id=header["id"])
        seen[header["id"]] = section
        records.append(JournalRecord(text=section, **{key: header[key] for key in _RECORD_FIELDS}))
    return records


@dataclass(frozen=True)
class AppendVerdict:
    accepted: bool
    reason: str = ""
    record_id: str = ""
    new_records: tuple[JournalRecord, ...] = ()


def check_append_only(accepted: Sequence[JournalRecord], candidate: Sequence[JournalRecord]) -> AppendVerdict:
    """Whether ``candidate`` extends ``accepted``: every accepted record present, byte-exact, in order,
    with only new records after them. A correction is a new record naming the one it corrects."""

    accepted = tuple(accepted)
    candidate = tuple(candidate)
    if len(candidate) < len(accepted):
        missing = accepted[len(candidate)].id
        return AppendVerdict(False, "journal_running_entry_rewritten", missing)
    for old, new in zip(accepted, candidate):
        if old.id != new.id or old.text != new.text:
            return AppendVerdict(False, "journal_running_entry_rewritten", old.id)
    known = {record.id for record in candidate}
    added = candidate[len(accepted):]
    for record in added:
        if record.corrects is not None and record.corrects not in known:
            return AppendVerdict(False, "journal_record_correction_target_missing", record.id)
    return AppendVerdict(True, new_records=added)


__all__ = [
    "AppendVerdict",
    "FEATURES_DIRECTORY",
    "JournalRecord",
    "RECORD_MARKER",
    "RUNNING_KIND",
    "RUNNING_SCHEMA",
    "check_append_only",
    "format_record",
    "normalize_feature",
    "parse_records",
    "record_id",
    "record_month",
    "running_entry_key",
    "running_entry_path",
    "running_entry_ref",
    "running_entry_semantic_name",
    "utc_instant",
]
