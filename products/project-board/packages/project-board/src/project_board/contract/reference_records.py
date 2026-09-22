from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from .errors import DomainError
from .refs import make_ref, make_worker_ref, normalize_reference_mapping, parse_ref


@dataclass(frozen=True)
class ReferencePolicy:
    id_field: str
    ref_field: str
    timestamp_fields: tuple[str, ...]
    semantic_fields: tuple[str, ...]
    semantic_fallback: str
    key_meaning: str
    semantic_meaning: str


# This table is the one decision point for durable URI parts. Call sites supply
# records; they do not get to invent a different key or label for the same kind.
REFERENCE_POLICIES: dict[str, ReferencePolicy] = {
    "assignment": ReferencePolicy(
        "assignment_id",
        "assignment_ref",
        ("assigned_at", "received_at", "created_at", "updated_at"),
        ("title", "subject"),
        "work-assignment",
        "the stable assignment row key",
        "the assigned work title",
    ),
    "call": ReferencePolicy(
        "call_id",
        "call_ref",
        ("opened_at", "created_at", "resolved_at"),
        ("subject",),
        "contested-call",
        "the stable contested-call row key",
        "the contested subject",
    ),
    "control": ReferencePolicy(
        "command_id",
        "command_ref",
        ("created_at", "available_at", "updated_at"),
        ("subject", "kind"),
        "control-command",
        "the stable command row key",
        "the command subject",
    ),
    "event": ReferencePolicy(
        "event_id",
        "event_ref",
        ("created_at", "recorded_at", "updated_at"),
        ("summary", "kind"),
        "service-event",
        "the stable event row key",
        "the event summary",
    ),
    "inbox": ReferencePolicy(
        "message_id",
        "message_ref",
        ("created_at", "updated_at"),
        ("subject", "kind"),
        "operator-message",
        "the stable operator-inbox row key",
        "the message subject",
    ),
    "journal": ReferencePolicy(
        "entry_id",
        "entry_ref",
        ("created_at", "recorded_at", "updated_at"),
        ("title", "summary", "status"),
        "journal-entry",
        "the stable journal entry key",
        "the journal title or summary",
    ),
    "journal_view": ReferencePolicy(
        "view_id",
        "view_ref",
        ("requested_at", "created_at", "updated_at"),
        ("title", "query", "mode"),
        "journal-view",
        "the stable journal-view row key",
        "the requested title, query, or mode",
    ),
    "mail": ReferencePolicy(
        "message_id",
        "message_ref",
        ("created_at", "updated_at"),
        ("subject", "kind"),
        "worker-message",
        "the stable local-message row key",
        "the message subject",
    ),
    "mail_reconciliation": ReferencePolicy(
        "receipt_id",
        "receipt_ref",
        ("started_at", "created_at", "completed_at"),
        ("title", "reporter_worker_name"),
        "mailbox-reconciliation",
        "the stable mailbox reconciliation receipt key",
        "the mailbox reconciliation run",
    ),
    "note": ReferencePolicy(
        "note_id",
        "note_ref",
        ("created_at", "updated_at"),
        ("text", "body", "author_kind"),
        "work-note",
        "the stable note row key",
        "the beginning of the note",
    ),
    "note_view": ReferencePolicy(
        "view_id",
        "view_ref",
        ("requested_at", "created_at", "updated_at"),
        ("title", "query", "mode"),
        "work-notes",
        "the stable note-view row key",
        "the requested note view",
    ),
    "plan": ReferencePolicy(
        "plan_id",
        "plan_ref",
        ("created_at", "updated_at"),
        ("title", "rationale", "status"),
        "dependency-plan",
        "the stable plan document key",
        "the plan title, rationale, or state",
    ),
    "report": ReferencePolicy(
        "report_id",
        "report_ref",
        (
            "requested_at",
            "recorded_at",
            "created_at",
            "published_at",
            "updated_at",
        ),
        ("ask", "summary_preview", "title"),
        "project-status-report",
        "the stable report row key",
        "the report request or summary",
    ),
    "review": ReferencePolicy(
        "review_id",
        "review_ref",
        ("created_at", "updated_at"),
        ("reason", "decision"),
        "work-review",
        "the stable review outcome key",
        "the review decision and reason",
    ),
    "session_resume": ReferencePolicy(
        "view_id",
        "view_ref",
        ("requested_at", "created_at", "updated_at"),
        ("runtime_kind", "title"),
        "session-resume",
        "the stable resume-view row key",
        "the resumed runtime",
    ),
}

DURABLE_REFERENCE_KINDS = frozenset(REFERENCE_POLICIES)

# Connection Hub grants share the ``work:`` namespace but are capabilities,
# not Problem Board object identities.  This grant also happens to satisfy the
# retired three-segment URI grammar, so generic stored-document scans must name
# the collision explicitly.
NON_REFERENCE_WORK_VALUES = frozenset({"work:journal:view"})


def _first_text(record: Mapping[str, Any], fields: Sequence[str]) -> str:
    for field in fields:
        value = record.get(field)
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            text = " ".join(str(item) for item in value if str(item or "").strip())
        else:
            text = str(value or "").strip()
        if text:
            return text
    return ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def reference_for_record(
    kind: str,
    record: Mapping[str, Any],
    *,
    require_stored_timestamp: bool = False,
) -> str:
    """Build the canonical URI for one durable record."""

    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind == "project":
        return make_ref("project", str(record.get("project_id") or ""))
    if normalized_kind == "worker":
        return make_worker_ref(
            record.get("runtime_kind"),
            record.get("runtime_session_id"),
        )
    policy = REFERENCE_POLICIES.get(normalized_kind)
    if policy is None:
        raise DomainError(
            "work_reference_policy_missing",
            "This Problem Board object kind has no reference policy.",
            details={"kind": normalized_kind},
        )
    timestamp = _first_text(record, policy.timestamp_fields)
    if not timestamp and require_stored_timestamp:
        raise DomainError(
            "work_reference_timestamp_missing",
            "A stored object cannot be migrated without its creation timestamp.",
            status=409,
            details={
                "kind": normalized_kind,
                "ref_field": policy.ref_field,
                "object_id": str(record.get(policy.id_field) or ""),
            },
        )
    return make_ref(
        normalized_kind,
        timestamp=timestamp or _now(),
        key=str(record.get(policy.id_field) or ""),
        semantic_name=(
            _first_text(record, policy.semantic_fields)
            or policy.semantic_fallback
        ),
    )


def reference_for_legacy_observation(
    source_ref: str,
    *,
    observed_at: Any,
) -> str:
    """Recover an ownerless legacy identity from its first durable observation.

    The caller must first exhaust authoritative record discovery and must limit
    recovery to a field that durably materialized the referenced object.  The
    kind's neutral label is intentional: a pointer does not own the original
    object's prose.
    """

    source = parse_ref(source_ref)
    policy = REFERENCE_POLICIES.get(source.kind)
    if source.is_canonical or policy is None:
        raise DomainError(
            "work_reference_observation_invalid",
            "Only a legacy durable reference can be recovered from an observation.",
            status=409,
            details={"source_ref": str(source_ref or "")},
        )
    return make_ref(
        source.kind,
        timestamp=observed_at,
        key=source.object_id,
        semantic_name=policy.semantic_fallback,
    )


def _records(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for key, item in value.items():
            yield from _records(key)
            yield from _records(item)
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        for item in value:
            yield from _records(item)


def _creation_timestamp_key(value: str) -> str:
    """Make second- and microsecond-precision URI timestamps comparable."""

    return value[:-1] + ("000000" if len(value) == 16 else "")


def _merge_identity_target(source: str, first: str, second: str) -> str:
    """Keep one creation identity across persisted lifecycle snapshots."""

    if first == second:
        return first
    first_ref = parse_ref(first)
    second_ref = parse_ref(second)
    if (
        first_ref.selector != second_ref.selector
        or first_ref.key != second_ref.key
        or first_ref.timestamp == second_ref.timestamp
    ):
        raise DomainError(
            "work_reference_mapping_ambiguous",
            "One legacy reference resolves to different canonical objects.",
            status=409,
            details={
                "source_ref": source,
                "first_target_ref": first,
                "second_target_ref": second,
            },
        )
    return min(
        (first, second),
        key=lambda target: _creation_timestamp_key(parse_ref(target).timestamp),
    )


def discover_reference_mapping(
    documents: Iterable[Any],
    *,
    kinds: Iterable[str] | None = None,
) -> dict[str, str]:
    """Find every authoritative legacy ref represented by the documents."""

    selected = set(kinds or REFERENCE_POLICIES)
    mapping: dict[str, str] = {}
    for document in documents:
        for record in _records(document):
            for kind in selected:
                policy = REFERENCE_POLICIES.get(kind)
                if policy is None:
                    continue
                source = str(record.get(policy.ref_field) or "").strip()
                if not source:
                    continue
                try:
                    parsed = parse_ref(source)
                except DomainError:
                    continue
                if parsed.kind != kind or parsed.is_canonical:
                    continue
                # A nested payload may repeat ``assignment_ref`` or another
                # pointer while carrying its own timestamp. Only the record
                # that also carries this kind's stable ID owns the identity.
                # Pointer copies are rewritten from that target later, or
                # reported unresolved when no owning record exists.
                object_id = str(record.get(policy.id_field) or "").strip()
                if not object_id or not _first_text(
                    record, policy.timestamp_fields
                ):
                    continue
                source_record = dict(record)
                target = reference_for_record(
                    kind,
                    source_record,
                    require_stored_timestamp=True,
                )
                previous = mapping.get(source)
                mapping[source] = (
                    target
                    if previous is None
                    else _merge_identity_target(source, previous, target)
                )
    return normalize_reference_mapping(mapping)


def merge_reference_mappings(*values: Mapping[str, str]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for value in values:
        for source, target in normalize_reference_mapping(value).items():
            previous = merged.get(source)
            merged[source] = (
                target
                if previous is None
                else _merge_identity_target(source, previous, target)
            )
    return normalize_reference_mapping(merged)


def referenced_legacy_refs(
    value: Any,
    *,
    kinds: Iterable[str] | None = None,
) -> set[str]:
    """Return exact legacy work refs still carried anywhere in a document."""

    selected = set(kinds or DURABLE_REFERENCE_KINDS)
    refs: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, str):
            if (
                not item.startswith("work:")
                or item in NON_REFERENCE_WORK_VALUES
            ):
                return
            try:
                parsed = parse_ref(item)
            except DomainError:
                return
            if parsed.kind in selected and not parsed.is_canonical:
                refs.add(item)
            return
        if isinstance(item, Mapping):
            for key, nested in item.items():
                visit(key)
                visit(nested)
            return
        if isinstance(item, Sequence) and not isinstance(
            item, (str, bytes, bytearray)
        ):
            for nested in item:
                visit(nested)

    visit(value)
    return refs


__all__ = [
    "DURABLE_REFERENCE_KINDS",
    "NON_REFERENCE_WORK_VALUES",
    "REFERENCE_POLICIES",
    "ReferencePolicy",
    "discover_reference_mapping",
    "merge_reference_mappings",
    "reference_for_legacy_observation",
    "reference_for_record",
    "referenced_legacy_refs",
]
