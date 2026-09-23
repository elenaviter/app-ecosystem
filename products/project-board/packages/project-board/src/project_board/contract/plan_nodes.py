from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Mapping

from .errors import DomainError
from .refs import (
    MAX_WORK_REF_BYTES,
    REFERENCE_KEY_SEGMENT_RE,
    REFERENCE_SEMANTIC_SEGMENT_RE,
    REFERENCE_STATE_VERSION_RE,
    REFERENCE_TIMESTAMP_RE,
    make_ref,
    normalize_semantic_name,
    parse_ref,
)


PLAN_NODE_PREFIX = "work:plan:node"
PLAN_NODE_TIMESTAMP_RE = REFERENCE_TIMESTAMP_RE
PLAN_NODE_KEY_SEGMENT_RE = REFERENCE_KEY_SEGMENT_RE
PLAN_NODE_SEGMENT_RE = REFERENCE_SEMANTIC_SEGMENT_RE
PLAN_NODE_STATE_VERSION_RE = REFERENCE_STATE_VERSION_RE
MAX_WORK_ITEM_REF_BYTES = MAX_WORK_REF_BYTES
LEGACY_TIMESTAMP_RE = re.compile(r"(?:^|_)([0-9]{8}T[0-9]{6}Z)(?:_[0-9a-f]{4})?$")
MAX_PLAN_REFERENCE_MAPPING_SIZE = 10_000
WORK_ITEM_AUTHORITATIVE_FIELDS = (
    "item_id",
    "item_key",
    "title",
    "tags",
    "keywords",
    "status",
    "assignee",
    "preferred_reworker",
    "depends_on",
    "ordinal",
    "created_at",
    "started_at",
    "cancelled_at",
    "cancelled_by",
    "note_count",
    "attachment_count",
    "body_hash",
    "body_bytes",
)
# Kept as a public alias while callers move to the name that states the
# contract: these fields define authoritative item state, not every column
# stored beside an item.
WORK_ITEM_VERSION_FIELDS = WORK_ITEM_AUTHORITATIVE_FIELDS
INLINE_WORK_ITEM_BODY_FIELDS = (
    "description",
    "acceptance",
    "attachment_refs",
    "result",
    "result_ref",
    "blocked_reason",
    "cancel_reason",
    "review",
    "review_requirement",
)


@dataclass(frozen=True)
class PlanNodeAddress:
    timestamp: str
    key: str
    semantic_name: str
    version: str | None = None

    @property
    def identity_ref(self) -> str:
        return ":".join(
            (PLAN_NODE_PREFIX, self.timestamp, self.key, self.semantic_name)
        )

    @property
    def ref(self) -> str:
        if self.version is None:
            return self.identity_ref
        return f"{self.identity_ref}:{self.version}"

    @property
    def version_timestamp(self) -> str | None:
        """Return the orienting timestamp from a legacy or current version."""

        if self.version is None:
            return None
        return self.version.split("-", 1)[0]

    @property
    def version_slug(self) -> str | None:
        """Return the collision-resistant suffix of a current version."""

        if self.version is None or "-" not in self.version:
            return None
        return self.version.split("-", 1)[1]

    @property
    def bucket(self) -> str:
        return f"{self.timestamp[:4]}-{self.timestamp[4:6]}"

    @property
    def prefix(self) -> str:
        return ":".join((PLAN_NODE_PREFIX, self.timestamp, self.key))


def _invalid(value: Any) -> DomainError:
    return DomainError(
        "work_ref_invalid",
        (
            "Expected work:plan:node:<created-at>:<key>:<semantic-name>, "
            "optionally followed by <version-timestamp>-<version-slug>."
        ),
        details={"object_ref": str(value or "")},
    )


def parse_plan_node_ref(value: Any) -> PlanNodeAddress:
    if not str(value or "").strip():
        raise DomainError(
            "work_value_required",
            "work_ref is required.",
            details={"field": "work_ref"},
        )
    try:
        parsed = parse_ref(value)
    except DomainError as exc:
        raise _invalid(value) from exc
    if parsed.selector != "plan:node" or not parsed.timestamp:
        raise _invalid(value)
    return PlanNodeAddress(
        parsed.timestamp,
        parsed.key,
        parsed.semantic_name,
        parsed.version,
    )


def plan_item_not_found_error(
    *,
    project_ref: str,
    work_ref: str = "",
    item_key: str = "",
    project_title: str = "",
    indexed_plan_revision: int | None = None,
    status: int = 404,
) -> DomainError:
    """Name the requested item and the authority in an absence response."""

    clean_project_ref = str(project_ref or "").strip()
    clean_work_ref = str(work_ref or "").strip()
    clean_item_key = str(item_key or "").strip()
    if clean_work_ref and not clean_item_key:
        try:
            clean_item_key = parse_plan_node_ref(clean_work_ref).key
        except DomainError:
            pass

    item_label = clean_item_key or clean_work_ref or "the requested work item"
    if clean_item_key and clean_work_ref:
        item_label = f"{clean_item_key} ({clean_work_ref})"
    clean_project_title = str(project_title or "").strip()
    project_label = clean_project_title or clean_project_ref
    if clean_project_title and clean_project_ref:
        project_label = f"{clean_project_title} ({clean_project_ref})"

    details: dict[str, Any] = {
        "project_ref": clean_project_ref,
        "work_ref": clean_work_ref,
        "item_key": clean_item_key,
        "absence_reason": "not_present_in_current_plan",
    }
    if indexed_plan_revision is not None:
        details["indexed_plan_revision"] = int(indexed_plan_revision)
    return DomainError(
        "work_plan_item_not_found",
        (
            f"Work item {item_label} is not present in the current plan for "
            f"project {project_label}."
        ),
        status=status,
        details=details,
    )


def make_plan_node_ref(
    *,
    timestamp: str,
    key: str,
    semantic_name: str,
    version: str | None = None,
    version_timestamp: str | None = None,
) -> str:
    if version and version_timestamp and version != version_timestamp:
        raise DomainError(
            "work_ref_version_conflict",
            "Supply one plan-node version value.",
        )
    return make_ref(
        "plan",
        subkind="node",
        timestamp=_timestamp_segment(timestamp),
        key=_key_segment(key),
        semantic_name=_semantic_segment(semantic_name, fallback="node"),
        version=_version_segment(version or version_timestamp) or "",
    )


def plan_node_ref_for_item(
    item: Mapping[str, Any],
    *,
    normalize_semantic_name: bool = False,
) -> str:
    existing = str(item.get("item_ref") or "").strip()
    if existing:
        try:
            address = parse_plan_node_ref(existing)
            if normalize_semantic_name:
                return make_plan_node_ref(
                    timestamp=address.timestamp,
                    key=address.key,
                    semantic_name=str(item.get("title") or address.semantic_name),
                )
            return address.identity_ref
        except DomainError:
            pass
    legacy_id = str(item.get("item_id") or "").strip()
    embedded = LEGACY_TIMESTAMP_RE.search(legacy_id)
    timestamp = embedded.group(1) if embedded else _timestamp_segment(
        str(item.get("created_at") or "")
    )
    return make_plan_node_ref(
        timestamp=timestamp,
        key=str(item.get("item_key") or "w"),
        semantic_name=str(item.get("title") or legacy_id or "node"),
    )


def plan_node_identity_ref(value: Any) -> str:
    """Return the immutable six-segment identity behind any live item URI."""

    return parse_plan_node_ref(value).identity_ref


def canonical_plan_node_key(value: Any) -> str:
    """Return the complete accepted key segment; invalid input is rejected."""

    return _key_segment(str(value or ""))


def work_item_authoritative_state(item: Mapping[str, Any]) -> dict[str, Any]:
    """Canonical state whose change is allowed to mint an item version.

    Body locators and retrieval-index columns are deliberately absent. They can
    be repaired or regenerated without changing the work a caller observes.
    When a local row still carries its body inline, the inline body is used in
    place of the persisted body hash.
    """

    identity_ref = plan_node_identity_ref(
        item.get("identity_ref") or item.get("item_ref")
    )
    dependencies = sorted(
        {
            plan_node_identity_ref(value)
            for value in item.get("depends_on") or ()
            if str(value or "").strip()
        }
    )
    body_hash = str(item.get("body_hash") or "")
    if body_hash:
        body: dict[str, Any] = {
            "hash": body_hash,
            "bytes": int(item.get("body_bytes") or 0),
        }
    else:
        body = {
            "inline": {
                field: _json_value(item.get(field))
                for field in INLINE_WORK_ITEM_BODY_FIELDS
            }
        }
    return {
        "identity_ref": identity_ref,
        "item_id": str(item.get("item_id") or ""),
        "item_key": str(item.get("item_key") or ""),
        "title": str(item.get("title") or ""),
        "tags": sorted({str(value) for value in item.get("tags") or ()}),
        "keywords": sorted(
            {str(value) for value in item.get("keywords") or ()}
        ),
        "status": str(item.get("status") or ""),
        "assignee": str(item.get("assignee") or ""),
        "depends_on": dependencies,
        "ordinal": int(item.get("ordinal") or 0),
        "created_at": _state_timestamp(item.get("created_at")),
        "started_at": _state_timestamp(item.get("started_at")),
        "cancelled_at": _state_timestamp(item.get("cancelled_at")),
        "cancelled_by": str(item.get("cancelled_by") or ""),
        "note_count": int(item.get("note_count") or 0),
        "attachment_count": int(item.get("attachment_count") or 0),
        "body": body,
    }


def work_item_authoritative_digest(item: Mapping[str, Any]) -> str:
    """Content digest for the canonical authoritative item state."""

    return hashlib.sha256(
        json.dumps(
            work_item_authoritative_state(item),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def work_item_version_token(item: Mapping[str, Any]) -> str:
    """Derive a stable ``<timestamp>-<slug>`` version for one item state."""

    revision = int(item.get("item_revision") or item.get("revision") or 0)
    if revision < 1:
        raise DomainError(
            "work_item_version_invalid",
            "A versioned work-item URI requires a positive item revision.",
        )
    identity_ref = plan_node_identity_ref(item.get("identity_ref") or item.get("item_ref"))
    identity = parse_plan_node_ref(identity_ref)
    item_updated_at = item.get("item_updated_at") or item.get("updated_at")
    version_timestamp = _version_timestamp_segment(
        item_updated_at,
        fallback=identity.timestamp,
    )
    state = {
        "revision": revision,
        # ``updated_at`` on the database row also changes for derived search
        # embeddings. The domain timestamp does not, so indexing and reads
        # cannot manufacture a new work-item version.
        "item_updated_at": _state_timestamp(item_updated_at),
        "authoritative_state": work_item_authoritative_state(
            {**dict(item), "identity_ref": identity_ref}
        ),
    }
    digest = hashlib.sha256(
        json.dumps(
            state,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"{version_timestamp}-{digest}"


def versioned_plan_node_ref(item: Mapping[str, Any]) -> str:
    """Project an authoritative item row as a versioned live URI."""

    address = parse_plan_node_ref(item.get("identity_ref") or item.get("item_ref"))
    return make_plan_node_ref(
        timestamp=address.timestamp,
        key=address.key,
        semantic_name=address.semantic_name,
        version=work_item_version_token(item),
    )


def is_plan_node_ref(value: Any) -> bool:
    try:
        parse_plan_node_ref(value)
    except DomainError:
        return False
    return True


def normalize_plan_reference_mapping(
    value: Mapping[str, Any] | None,
    *,
    target_refs: set[str] | None = None,
) -> dict[str, str]:
    """Validate a bounded legacy-reference map whose targets are final URIs."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise DomainError(
            "plan_import_reference_mapping_invalid",
            "The plan reference mapping must be an object.",
        )
    if len(value) > MAX_PLAN_REFERENCE_MAPPING_SIZE:
        raise DomainError(
            "plan_import_reference_mapping_too_large",
            "The plan reference mapping exceeds the guarded import limit.",
            status=409,
            details={
                "mapping_count": len(value),
                "maximum": MAX_PLAN_REFERENCE_MAPPING_SIZE,
            },
        )
    normalized: dict[str, str] = {}
    for raw_source, raw_target in value.items():
        source = str(raw_source or "").strip()
        target = str(raw_target or "").strip()
        if not source or len(source.encode("utf-8")) > 512:
            raise DomainError(
                "plan_import_reference_source_invalid",
                "A plan reference mapping source is empty or too large.",
                status=409,
            )
        try:
            target_address = parse_plan_node_ref(target)
        except DomainError as exc:
            raise DomainError(
                "plan_import_reference_target_invalid",
                "Every plan reference mapping target must be a canonical plan-node URI.",
                status=409,
                details={"source_ref": source, "target_ref": target},
            ) from exc
        if target_address.version is not None:
            raise DomainError(
                "plan_import_reference_target_versioned",
                "A plan identity migration target must use an unversioned plan-node URI.",
                status=409,
                details={"source_ref": source, "target_ref": target},
            )
        target = target_address.identity_ref
        try:
            source_address = parse_plan_node_ref(source)
        except DomainError:
            source_address = None
        if source_address is not None and source_address.version is not None:
            raise DomainError(
                "plan_import_reference_source_versioned",
                "A plan identity migration source must use an unversioned plan-node URI.",
                status=409,
                details={"source_ref": source, "target_ref": target},
            )
        if source == target:
            raise DomainError(
                "plan_import_reference_identity_invalid",
                "A plan reference mapping must describe an actual URI change.",
                status=409,
                details={"work_ref": source},
            )
        normalized[source] = target

    chained = sorted(set(normalized.values()) & set(normalized))
    if chained:
        raise DomainError(
            "plan_import_reference_mapping_unresolved",
            "The plan reference mapping must resolve every source directly to its final URI.",
            status=409,
            details={"intermediate_refs": chained[:20]},
        )
    if target_refs is not None:
        missing = sorted(set(normalized.values()) - set(target_refs))
        if missing:
            raise DomainError(
                "plan_import_reference_target_missing",
                "A plan reference mapping target is outside the imported plan.",
                status=409,
                details={"target_refs": missing[:20]},
            )
        canonical_sources = sorted(set(normalized) & set(target_refs))
        if canonical_sources:
            raise DomainError(
                "plan_import_reference_source_conflict",
                "A current plan-node URI cannot also be a migration source.",
                status=409,
                details={"work_refs": canonical_sources[:20]},
            )
    return dict(sorted(normalized.items()))


def _timestamp_segment(value: str) -> str:
    text = str(value or "").strip()
    if PLAN_NODE_TIMESTAMP_RE.fullmatch(text):
        return text
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DomainError(
            "field_timestamp_invalid",
            "A plan node needs a valid creation timestamp.",
            details={"created_at": text},
        ) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _version_segment(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if (
        PLAN_NODE_TIMESTAMP_RE.fullmatch(text) is None
        and PLAN_NODE_STATE_VERSION_RE.fullmatch(text) is None
    ):
        raise _invalid(value)
    return text


def _version_timestamp_segment(value: Any, *, fallback: str) -> str:
    """Return an orienting state timestamp without weakening exact identity.

    Older imported rows and test doubles can carry an opaque update marker.
    The SHA-256 suffix still names their exact state, so the immutable creation
    timestamp is a truthful fallback for the human-readable timestamp portion.
    """

    text = str(value or "").strip()
    if text:
        try:
            return _timestamp_segment(text)
        except DomainError:
            pass
    return _timestamp_segment(fallback)


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((_json_value(item) for item in value), key=repr)
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _state_timestamp(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return _json_value(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _semantic_segment(
    value: str,
    *,
    fallback: str,
) -> str:
    return normalize_semantic_name(value, fallback=fallback)


def _key_segment(value: str) -> str:
    """Canonicalize a complete accepted item key without silent truncation."""

    segment = "-".join(
        word
        for word in re.split(r"[^0-9A-Za-z]+", str(value or "").lower())
        if word
    )
    if not segment:
        segment = "w"
    if PLAN_NODE_KEY_SEGMENT_RE.fullmatch(segment) is None:
        raise DomainError(
            "work_ref_key_invalid",
            "A plan node key must produce a canonical URI segment of at most 128 characters.",
            details={"value": str(value or "")[:256], "maximum_characters": 128},
        )
    return segment


__all__ = [
    "LEGACY_TIMESTAMP_RE",
    "MAX_PLAN_REFERENCE_MAPPING_SIZE",
    "MAX_WORK_ITEM_REF_BYTES",
    "PLAN_NODE_KEY_SEGMENT_RE",
    "PLAN_NODE_PREFIX",
    "PLAN_NODE_SEGMENT_RE",
    "PLAN_NODE_STATE_VERSION_RE",
    "PLAN_NODE_TIMESTAMP_RE",
    "WORK_ITEM_AUTHORITATIVE_FIELDS",
    "WORK_ITEM_VERSION_FIELDS",
    "PlanNodeAddress",
    "canonical_plan_node_key",
    "is_plan_node_ref",
    "make_plan_node_ref",
    "normalize_plan_reference_mapping",
    "parse_plan_node_ref",
    "plan_node_identity_ref",
    "plan_node_ref_for_item",
    "versioned_plan_node_ref",
    "work_item_authoritative_digest",
    "work_item_authoritative_state",
    "work_item_version_token",
]
