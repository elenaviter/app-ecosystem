from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from ..contract.errors import DomainError


COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    text = str(value or "").strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise DomainError("field_timestamp_invalid", "The stored timestamp is invalid.") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def new_id(prefix: str) -> str:
    return f"{component(prefix)}_{uuid.uuid4().hex}"


# Words that carry no meaning in a four-word label. Dropping them is what turns
# "show-the-project-moving-not-just" into "show-project-moving".
_STEM_FILLER = frozenset(
    """a an the and or but of to in on at by for from with without into over under
    is are was were be been being it its this that these those not just what which
    while when than then so as if""".split()
)


def id_stem(text: str, *, fallback: str = "item", words: int = 4, maximum: int = 32) -> str:
    """A short readable stem for an id, derived from a title.

    Content words only, lowercase, hyphen joined, at most a handful. This is a
    hint for a human reading a ref, so it is better short and slightly wrong
    than long and exact. When a caller has a real label for the thing, it should
    pass that instead of leaning on this.
    """

    parts = [word for word in re.split(r"[^0-9A-Za-z]+", str(text or "").lower()) if word]
    kept = [word for word in parts if word not in _STEM_FILLER] or parts
    stem = ""
    for word in kept[:words]:
        candidate = f"{stem}-{word}" if stem else word
        if len(candidate) > maximum:
            break
        stem = candidate
    return stem or fallback


def new_timed_id(prefix: str) -> str:
    """A generated id that says when it was made and, where possible, what of.

    The shape is ``<stem>_<UTC stamp>``, as in
    ``progress-rollup_20260913T013000Z``. Both halves earn their place.

    The stamp is the part that cannot be argued with. It is a record of the
    generation itself, it sorts a directory listing or a set of refs into the
    order things happened without reading any of them, and it is what makes the
    id an identifier rather than a name. The four hex characters after it are
    there because a stamp in seconds is not unique: two calls recorded in the
    same second collided and one silently replaced the other, which a test
    caught and a reader never would.

    The stem is there because an id nobody can read makes every message, graph
    node, and log line that carries it opaque. It is a hint and never a key:
    lookups resolve the whole id, so a title that changes later leaves the stem
    stale without breaking anything, and the stamp beside it says how old that
    wording is.

    What this replaces is an id that was only a name. One plan node still
    carries ``participation-service``, a name withdrawn the same day and now
    welded into every event, journal entry, and message that mentions it. The
    weld was not caused by the words being readable. It was caused by the words
    being the entire identifier, so they read as the truth and were resolved by.
    A stamped id has a hint that can go stale and a key that cannot.
    """

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{component(prefix)}_{stamp}_{uuid.uuid4().hex[:4]}"


def new_keyed_id(key: str, title: str, *, fallback: str = "item") -> str:
    """An id that answers where, what, and when without a lookup.

    ``w41_catalog-fragment-apply_20260912T233233Z``: the ordered key the board
    already shows, the words from the title, and the moment it was generated.

    The key comes first because it is what a person says out loud and what the
    board displays, so a ref and a conversation agree. It is allocated unique
    within its plan, which is what makes a random suffix unnecessary: two items
    cannot share a key, so two ids cannot collide however fast they are created.

    A slug alone was never unique, sorted by nothing, and said nothing about
    when it was written. One plan node still reads ``progress-rollup``, which is
    a name that happened not to clash yet.
    """

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return "_".join(
        (
            id_stem(key, fallback="w"),
            id_stem(title, fallback=fallback),
            stamp,
        )
    )


def new_named_id(title: str, *, fallback: str = "item") -> str:
    """The id for something that has a title, stemmed from that title."""

    return new_timed_id(id_stem(title, fallback=fallback))


def component(value: Any, *, field: str = "id") -> str:
    text = str(value or "").strip()
    if not COMPONENT_RE.fullmatch(text):
        raise DomainError(
            "field_component_invalid",
            f"{field} must contain only letters, digits, dot, underscore, or hyphen.",
            details={"field": field},
        )
    return text


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def bounded_text(value: Any, *, field: str, maximum: int, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise DomainError("field_value_required", f"{field} is required.", details={"field": field})
    if len(text.encode("utf-8")) > maximum:
        raise DomainError(
            "field_value_too_large",
            f"{field} exceeds the {maximum}-byte local protocol limit.",
            details={"field": field, "maximum_bytes": maximum},
        )
    return text


def read_json(path: Path, *, required: bool = True) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if not required:
            return {}
        raise DomainError(
            "field_record_not_found",
            "The requested shared-field record does not exist.",
            status=404,
            details={"path": str(path)},
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise DomainError(
            "field_record_unreadable",
            "The shared-field record could not be read.",
            details={"path": str(path)},
        ) from exc
    if not isinstance(value, Mapping):
        raise DomainError(
            "field_record_invalid",
            "The shared-field record must be a JSON object.",
            details={"path": str(path)},
        )
    return dict(value)


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps(dict(value), ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            os.chmod(temporary, 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            os.chmod(temporary, 0o600)
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise OSError("This platform cannot safely open lock files without following links.")
    descriptor = os.open(
        path,
        os.O_RDWR | os.O_CREAT | os.O_APPEND | no_follow,
        0o600,
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"Lock path is not a regular file: {path}")
        os.fchmod(descriptor, 0o600)
        handle = os.fdopen(descriptor, "a+", encoding="utf-8")
        descriptor = -1
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    with handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def json_records(directory: Path) -> list[dict[str, Any]]:
    if not directory.exists():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        records.append(read_json(path))
    return records


def newest_json_records(
    directory: Path,
    *,
    limit: int,
    predicate: Callable[[Mapping[str, Any]], bool] | None = None,
) -> list[dict[str, Any]]:
    """Read only the newest bounded slice of an append-only JSON directory.

    File modification time is the ordering authority for these local derived
    records. Selecting paths before parsing keeps a growing history from making
    every current-state read progressively slower, while each returned record
    is still read from disk on every call.
    """

    if not directory.exists() or int(limit) <= 0:
        return []
    paths = sorted(
        directory.glob("*.json"),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    records: list[dict[str, Any]] = []
    for path in paths:
        record = read_json(path)
        if predicate is not None and not predicate(record):
            continue
        records.append(record)
        if len(records) >= int(limit):
            break
    return records


__all__ = [
    "new_keyed_id",
    "atomic_write_json",
    "atomic_write_text",
    "bounded_text",
    "canonical_json",
    "component",
    "content_hash",
    "exclusive_lock",
    "json_records",
    "newest_json_records",
    "new_id",
    "parse_utc",
    "read_json",
    "utc_now",
]
