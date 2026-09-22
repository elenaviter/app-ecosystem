from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any, Mapping

from .errors import DomainError


NAMESPACE = "work"
KINDS = {
    "project": "work.project",
    "dependency": "work.dependency",
    "plan": "work.plan",
    "worker": "work.worker",
    "mail": "work.mail",
    "mail_reconciliation": "work.mail_reconciliation",
    "lease": "work.lease",
    "journal": "work.journal",
    "journal_view": "work.journal_view",
    "session_resume": "work.session_resume",
    "inbox": "work.inbox",
    "event": "work.event",
    "control": "work.control",
    "assignment": "work.assignment",
    "note": "work.note",
    "note_view": "work.note_view",
    "report": "work.report",
    "review": "work.review",
    "call": "work.call",
}

# A kind that addresses things *inside* another kind names them here, so a ref
# can say what a thing is rather than leaving it to be inferred.
SUBKINDS = {
    "plan": {"node": "work.plan.node"},
}

MAX_WORK_REF_BYTES = 512
REFERENCE_TIMESTAMP_RE = re.compile(
    r"^[0-9]{8}T[0-9]{6}(?:[0-9]{6})?Z$"
)
REFERENCE_KEY_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
REFERENCE_SEMANTIC_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
REFERENCE_STATE_VERSION_RE = re.compile(
    r"^[0-9]{8}T[0-9]{6}(?:[0-9]{6})?Z-[0-9a-f]{64}$"
)


@dataclass(frozen=True)
class WorkRef:
    kind: str
    object_id: str
    subkind: str = ""
    timestamp: str = ""
    key: str = ""
    semantic_name: str = ""
    version: str | None = None
    shape: str = "legacy"

    @property
    def object_kind(self) -> str:
        if self.subkind:
            return SUBKINDS[self.kind][self.subkind]
        return KINDS[self.kind]

    @property
    def selector(self) -> str:
        """What this ref addresses, as one comparable token."""

        return f"{self.kind}:{self.subkind}" if self.subkind else self.kind

    @property
    def is_canonical(self) -> bool:
        return self.shape != "legacy"

    @property
    def identity_ref(self) -> str:
        if self.version is None:
            return str(self)
        return str(
            WorkRef(
                kind=self.kind,
                object_id=self.object_id,
                subkind=self.subkind,
                timestamp=self.timestamp,
                key=self.key,
                semantic_name=self.semantic_name,
                shape=self.shape,
            )
        )

    def __str__(self) -> str:
        prefix = f"{NAMESPACE}:{self.kind}"
        if self.subkind:
            prefix = f"{prefix}:{self.subkind}"
        if self.shape == "worker":
            return f"{prefix}:{self.key}:{self.semantic_name}"
        if self.timestamp:
            value = f"{prefix}:{self.timestamp}:{self.key}:{self.semantic_name}"
            return f"{value}:{self.version}" if self.version else value
        return f"{prefix}:{self.object_id}"


def _invalid(value: Any) -> DomainError:
    return DomainError(
        "work_ref_invalid",
        (
            "Expected a canonical Problem Board reference: a natural project "
            "identity, a worker runtime plus native session identity, or "
            "work:<kind>[:<subkind>]:<created-at>:<key>:<semantic-name>."
        ),
        details={"object_ref": str(value or "")},
    )


def normalize_reference_timestamp(value: Any) -> str:
    text = str(value or "").strip()
    if REFERENCE_TIMESTAMP_RE.fullmatch(text):
        return text
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DomainError(
            "field_timestamp_invalid",
            "A Problem Board reference needs a valid creation timestamp.",
            details={"created_at": text},
        ) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    if moment.microsecond:
        return moment.strftime("%Y%m%dT%H%M%S%fZ")
    return moment.strftime("%Y%m%dT%H%M%SZ")


def normalize_reference_key(value: Any) -> str:
    text = str(value or "").strip()
    if REFERENCE_KEY_SEGMENT_RE.fullmatch(text) is None:
        raise DomainError(
            "work_ref_key_invalid",
            "A Problem Board reference key must be one segment of at most 128 characters.",
            details={"value": text[:256], "maximum_characters": 128},
        )
    return text


def normalize_semantic_name(value: Any, *, fallback: str) -> str:
    words = [
        word
        for word in re.split(r"[^0-9A-Za-z]+", str(value or "").lower())
        if word
    ]
    selected: list[str] = []
    for word in words:
        candidate = "-".join((*selected, word))
        if len(candidate) > 64:
            break
        selected.append(word)
    segment = "-".join(selected) or str(fallback or "object").strip().lower()
    if REFERENCE_SEMANTIC_SEGMENT_RE.fullmatch(segment) is None:
        raise DomainError(
            "work_ref_semantic_name_invalid",
            "A Problem Board reference semantic name could not be derived.",
            details={"value": str(value or "")[:128]},
        )
    return segment


def make_ref(
    kind: str,
    object_id: str = "",
    subkind: str = "",
    *,
    timestamp: Any = "",
    key: Any = "",
    semantic_name: Any = "",
    version: Any = "",
) -> str:
    """Build one Problem Board reference through the shared grammar.

    ``object_id`` remains accepted for natural project identities and rolling
    legacy callers. New durable records pass ``timestamp``, ``key`` and
    ``semantic_name`` together. Their key is the row's stable lookup key; the
    URI adds when the row was created and what a person knows it as.
    """

    normalized_kind = str(kind or "").strip().lower()
    normalized_sub = str(subkind or "").strip().lower()
    normalized_id = str(object_id or "").strip()
    if normalized_kind not in KINDS:
        raise _invalid(f"{NAMESPACE}:{normalized_kind}:{normalized_id}")
    if normalized_sub and normalized_sub not in SUBKINDS.get(normalized_kind, {}):
        raise _invalid(f"{NAMESPACE}:{normalized_kind}:{normalized_sub}:{normalized_id}")

    supplied_semantic_parts = any(
        str(value or "").strip() for value in (timestamp, key, semantic_name, version)
    )
    if supplied_semantic_parts:
        if normalized_id or not all(
            str(value or "").strip() for value in (timestamp, key, semantic_name)
        ):
            raise _invalid(normalized_id or key)
        normalized_timestamp = normalize_reference_timestamp(timestamp)
        normalized_key = normalize_reference_key(key)
        normalized_name = normalize_semantic_name(
            semantic_name,
            fallback=normalized_sub or normalized_kind,
        )
        normalized_version = str(version or "").strip() or None
        if normalized_version is not None and (
            normalized_kind != "plan"
            or normalized_sub != "node"
            or (
                REFERENCE_TIMESTAMP_RE.fullmatch(normalized_version) is None
                and REFERENCE_STATE_VERSION_RE.fullmatch(normalized_version) is None
            )
        ):
            raise _invalid(version)
        return str(
            WorkRef(
                kind=normalized_kind,
                object_id=normalized_key,
                subkind=normalized_sub,
                timestamp=normalized_timestamp,
                key=normalized_key,
                semantic_name=normalized_name,
                version=normalized_version,
                shape="semantic",
            )
        )

    valid_legacy = (
        normalized_id
        and not normalized_sub
        and ":" not in normalized_id
        and normalized_id not in SUBKINDS.get(normalized_kind, {})
    )
    if not valid_legacy:
        raise _invalid(normalized_id)
    shape = "natural" if normalized_kind == "project" and not normalized_sub else "legacy"
    return str(WorkRef(normalized_kind, normalized_id, normalized_sub, shape=shape))


def make_worker_ref(runtime_kind: Any, runtime_session_id: Any) -> str:
    from .worker_identity import WorkerSessionIdentity

    identity = WorkerSessionIdentity.create(runtime_kind, runtime_session_id)
    return str(
        WorkRef(
            kind="worker",
            object_id=identity.worker_name,
            key=identity.runtime_kind,
            semantic_name=identity.runtime_session_id,
            shape="worker",
        )
    )


def parse_ref(value: Any) -> WorkRef:
    text = str(value or "").strip()
    if not text or len(text.encode("utf-8")) > MAX_WORK_REF_BYTES:
        raise _invalid(value)
    parts = text.split(":")
    if len(parts) < 3 or parts[0] != NAMESPACE or parts[1] not in KINDS:
        raise _invalid(value)
    kind = parts[1]
    subkind = ""
    payload = parts[2:]
    subkinds = SUBKINDS.get(kind, {})
    if payload and payload[0] in subkinds:
        subkind = payload.pop(0)

    if kind == "worker" and not subkind and len(payload) == 2:
        from .worker_identity import WorkerSessionIdentity

        identity = WorkerSessionIdentity.create(payload[0], payload[1])
        return WorkRef(
            kind=kind,
            object_id=identity.worker_name,
            key=identity.runtime_kind,
            semantic_name=identity.runtime_session_id,
            shape="worker",
        )

    if len(payload) in {3, 4}:
        timestamp, key, semantic_name = payload[:3]
        version = payload[3] if len(payload) == 4 else None
        if (
            REFERENCE_TIMESTAMP_RE.fullmatch(timestamp) is None
            or REFERENCE_KEY_SEGMENT_RE.fullmatch(key) is None
            or REFERENCE_SEMANTIC_SEGMENT_RE.fullmatch(semantic_name) is None
            or (
                version is not None
                and (
                    kind != "plan"
                    or subkind != "node"
                    or (
                        REFERENCE_TIMESTAMP_RE.fullmatch(version) is None
                        and REFERENCE_STATE_VERSION_RE.fullmatch(version) is None
                    )
                )
            )
        ):
            raise _invalid(value)
        return WorkRef(
            kind=kind,
            object_id=key,
            subkind=subkind,
            timestamp=timestamp,
            key=key,
            semantic_name=semantic_name,
            version=version,
            shape="semantic",
        )

    if (
        len(payload) != 1
        or not payload[0]
        or payload[0] in subkinds
        or bool(subkind)
    ):
        raise _invalid(value)
    return WorkRef(
        kind=kind,
        object_id=payload[0],
        subkind=subkind,
        shape="natural" if kind == "project" and not subkind else "legacy",
    )


def normalize_reference_mapping(
    value: Mapping[str, Any] | None,
    *,
    maximum: int = 10_000,
) -> dict[str, str]:
    """Validate a bounded map whose targets are canonical final references."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise DomainError(
            "work_reference_mapping_invalid",
            "The reference mapping must be an object.",
        )
    if len(value) > maximum:
        raise DomainError(
            "work_reference_mapping_too_large",
            "The reference mapping exceeds the guarded migration limit.",
            status=409,
            details={"mapping_count": len(value), "maximum": maximum},
        )
    normalized: dict[str, str] = {}
    for raw_source, raw_target in value.items():
        source = str(raw_source or "").strip()
        target = str(raw_target or "").strip()
        if not source or len(source.encode("utf-8")) > MAX_WORK_REF_BYTES:
            raise DomainError(
                "work_reference_mapping_source_invalid",
                "A reference mapping source is empty or too large.",
                status=409,
            )
        target_address = parse_ref(target)
        try:
            source_address = parse_ref(source)
        except DomainError:
            source_address = None
        if (
            source_address is not None
            and source_address.selector != target_address.selector
        ):
            raise DomainError(
                "work_reference_mapping_kind_mismatch",
                "A reference mapping cannot change the kind of the addressed object.",
                status=409,
                details={
                    "source_ref": source,
                    "target_ref": target,
                },
            )
        if not target_address.is_canonical:
            raise DomainError(
                "work_reference_mapping_target_legacy",
                "Every reference mapping target must use a canonical Problem Board URI.",
                status=409,
                details={"source_ref": source, "target_ref": target},
            )
        if target_address.version is not None:
            raise DomainError(
                "work_reference_mapping_target_versioned",
                "A reference identity migration target cannot carry an observed-state version.",
                status=409,
                details={"source_ref": source, "target_ref": target},
            )
        if source == target:
            raise DomainError(
                "work_reference_mapping_identity_invalid",
                "A reference mapping must describe an actual URI change.",
                status=409,
                details={"work_ref": source},
            )
        normalized[source] = target
    chained = sorted(set(normalized.values()) & set(normalized))
    if chained:
        raise DomainError(
            "work_reference_mapping_unresolved",
            "Every source must resolve directly to its final URI.",
            status=409,
            details={"intermediate_refs": chained[:20]},
        )
    return dict(sorted(normalized.items()))


def object_kind_for_ref(value: str) -> str | None:
    try:
        return parse_ref(value).object_kind
    except DomainError:
        return None


__all__ = [
    "KINDS",
    "MAX_WORK_REF_BYTES",
    "NAMESPACE",
    "REFERENCE_KEY_SEGMENT_RE",
    "REFERENCE_SEMANTIC_SEGMENT_RE",
    "REFERENCE_STATE_VERSION_RE",
    "REFERENCE_TIMESTAMP_RE",
    "SUBKINDS",
    "WorkRef",
    "make_ref",
    "make_worker_ref",
    "normalize_reference_key",
    "normalize_reference_mapping",
    "normalize_reference_timestamp",
    "normalize_semantic_name",
    "object_kind_for_ref",
    "parse_ref",
]
