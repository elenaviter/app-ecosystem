from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from .errors import DomainError
from .refs import normalize_reference_mapping


REFERENCE_MAPPING_DOCUMENT_SCHEMA = "problem-board.reference-mapping-document.v1"
REFERENCE_MAPPING_ARTIFACT_SCHEMA = "problem-board.reference-mapping-artifact.v1"
MAX_REFERENCE_MAPPING_DOCUMENT_BYTES = 25 * 1024 * 1024


def encode_reference_mapping_document(
    *,
    project_ref: str,
    reference_mapping: Mapping[str, Any],
) -> bytes:
    mapping = normalize_reference_mapping(reference_mapping)
    document = {
        "schema": REFERENCE_MAPPING_DOCUMENT_SCHEMA,
        "project_ref": str(project_ref or "").strip(),
        "reference_mapping_hash": _hash(mapping),
        "reference_mapping_count": len(mapping),
        "reference_mapping": mapping,
    }
    encoded = _canonical(document)
    if len(encoded) > MAX_REFERENCE_MAPPING_DOCUMENT_BYTES:
        raise DomainError(
            "work_reference_mapping_document_too_large",
            "The reference mapping document exceeds the guarded artifact limit.",
            status=409,
            details={
                "document_bytes": len(encoded),
                "maximum_bytes": MAX_REFERENCE_MAPPING_DOCUMENT_BYTES,
            },
        )
    return encoded


def decode_reference_mapping_document(
    encoded: bytes,
    *,
    project_ref: str,
    expected_content_hash: str = "",
    expected_content_bytes: Any = None,
    expected_mapping_hash: str = "",
    expected_mapping_count: Any = None,
) -> dict[str, str]:
    if not isinstance(encoded, bytes):
        raise DomainError(
            "work_reference_mapping_artifact_invalid",
            "Reference mapping storage returned a non-binary document.",
            status=502,
        )
    if len(encoded) > MAX_REFERENCE_MAPPING_DOCUMENT_BYTES:
        raise DomainError(
            "work_reference_mapping_document_too_large",
            "The reference mapping document exceeds the guarded artifact limit.",
            status=409,
            details={
                "document_bytes": len(encoded),
                "maximum_bytes": MAX_REFERENCE_MAPPING_DOCUMENT_BYTES,
            },
        )
    if expected_content_bytes is not None and len(encoded) != _non_negative_int(
        expected_content_bytes,
        field="content_bytes",
    ):
        raise DomainError(
            "work_reference_mapping_artifact_changed",
            "The reference mapping artifact size differs from the reviewed artifact.",
            status=409,
        )
    actual_content_hash = hashlib.sha256(encoded).hexdigest()
    if expected_content_hash and actual_content_hash != str(expected_content_hash):
        raise DomainError(
            "work_reference_mapping_artifact_changed",
            "The reference mapping artifact does not match its reviewed hash.",
            status=409,
        )
    try:
        document = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DomainError(
            "work_reference_mapping_artifact_invalid",
            "The reference mapping artifact is not valid JSON.",
            status=409,
        ) from exc
    if (
        not isinstance(document, Mapping)
        or document.get("schema") != REFERENCE_MAPPING_DOCUMENT_SCHEMA
    ):
        raise DomainError(
            "work_reference_mapping_artifact_invalid",
            "The reference mapping artifact has an unsupported schema.",
            status=409,
        )
    if str(document.get("project_ref") or "") != str(project_ref or "").strip():
        raise DomainError(
            "work_reference_mapping_artifact_identity_mismatch",
            "The reference mapping artifact belongs to another project.",
            status=409,
        )
    mapping = normalize_reference_mapping(document.get("reference_mapping"))
    mapping_hash = _hash(mapping)
    mapping_count = len(mapping)
    document_mapping_count = _non_negative_int(
        document.get("reference_mapping_count"),
        field="reference_mapping_count",
    )
    if (
        str(document.get("reference_mapping_hash") or "") != mapping_hash
        or document_mapping_count != mapping_count
    ):
        raise DomainError(
            "work_reference_mapping_artifact_invalid",
            "The reference mapping artifact metadata does not match its mapping.",
            status=409,
        )
    if expected_mapping_hash and mapping_hash != str(expected_mapping_hash):
        raise DomainError(
            "work_reference_mapping_artifact_changed",
            "The reference mapping differs from the reviewed mapping hash.",
            status=409,
        )
    if expected_mapping_count is not None and mapping_count != _non_negative_int(
        expected_mapping_count,
        field="reference_mapping_count",
    ):
        raise DomainError(
            "work_reference_mapping_artifact_changed",
            "The reference mapping count differs from the reviewed artifact.",
            status=409,
        )
    return mapping


class ReferenceMappingArtifacts:
    """Move complete migration maps outside the bounded control envelope."""

    def __init__(self, attachments: Any) -> None:
        self.attachments = attachments

    async def consume_upload(
        self,
        *,
        project_ref: str,
        upload: Mapping[str, Any],
    ) -> dict[str, str]:
        staged_ref = _required(upload, "staged_ref")
        # Preview may be re-executed with the same transport identity after an
        # uncertain Data Bus outcome.  Its integrity-bound input remains in
        # staging until the normal TTL sweep so that retry reads the same bytes.
        _filename, _mime, encoded = await self.attachments.read_staged_bytes(
            staged_ref=staged_ref
        )
        return decode_reference_mapping_document(
            encoded,
            project_ref=project_ref,
            expected_content_hash=_required(upload, "content_hash"),
            expected_content_bytes=_required_int(upload, "content_bytes"),
            expected_mapping_hash=_required(upload, "reference_mapping_hash"),
            expected_mapping_count=_required_int(
                upload, "reference_mapping_count"
            ),
        )

    async def persist(
        self,
        *,
        project_ref: str,
        owner_id: str,
        reference_mapping: Mapping[str, Any],
    ) -> dict[str, Any]:
        mapping = normalize_reference_mapping(reference_mapping)
        encoded = encode_reference_mapping_document(
            project_ref=project_ref,
            reference_mapping=mapping,
        )
        content_hash = hashlib.sha256(encoded).hexdigest()
        stored = await self.attachments.store_bytes_on_turn(
            filename=f"reference-mapping-{content_hash}.json",
            data=encoded,
            mime="application/json",
            owner_id=owner_id,
            conversation_id="reference-migrations",
            turn_id=content_hash,
            role="artifact",
            origin="worker",
        )
        download_url = await self.attachments.signed_download_url(
            stored,
            requester_id=owner_id,
        )
        file_ref = str(stored.get("file_ref") or "").strip()
        if not file_ref or not str(download_url or "").strip():
            raise DomainError(
                "work_reference_mapping_storage_unavailable",
                "Reference mapping storage did not return a stable ref and signed read link.",
                status=503,
            )
        return {
            "schema": REFERENCE_MAPPING_ARTIFACT_SCHEMA,
            "project_ref": str(project_ref),
            "file_ref": file_ref,
            "content_hash": content_hash,
            "content_bytes": len(encoded),
            "reference_mapping_hash": _hash(mapping),
            "reference_mapping_count": len(mapping),
            "download_url": str(download_url),
        }

    async def load(
        self,
        *,
        project_ref: str,
        artifact: Mapping[str, Any],
    ) -> dict[str, str]:
        if artifact.get("schema") != REFERENCE_MAPPING_ARTIFACT_SCHEMA:
            raise DomainError(
                "work_reference_mapping_artifact_invalid",
                "The reference mapping artifact descriptor has an unsupported schema.",
                status=409,
            )
        if str(artifact.get("project_ref") or "") != str(project_ref):
            raise DomainError(
                "work_reference_mapping_artifact_identity_mismatch",
                "The reference mapping artifact belongs to another project.",
                status=409,
            )
        _filename, _mime, encoded = await self.attachments.read_stored(
            file_ref_value=_required(artifact, "file_ref")
        )
        return decode_reference_mapping_document(
            encoded,
            project_ref=project_ref,
            expected_content_hash=_required(artifact, "content_hash"),
            expected_content_bytes=_required_int(artifact, "content_bytes"),
            expected_mapping_hash=_required(
                artifact, "reference_mapping_hash"
            ),
            expected_mapping_count=_required_int(
                artifact, "reference_mapping_count"
            ),
        )


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _required(value: Mapping[str, Any], field: str) -> str:
    text = str(value.get(field) or "").strip()
    if not text:
        raise DomainError(
            "work_reference_mapping_artifact_invalid",
            f"The reference mapping artifact requires {field}.",
            status=409,
            details={"field": field},
        )
    return text


def _required_int(value: Mapping[str, Any], field: str) -> int:
    if field not in value:
        raise DomainError(
            "work_reference_mapping_artifact_invalid",
            f"The reference mapping artifact requires integer {field}.",
            status=409,
            details={"field": field},
        )
    return _non_negative_int(value.get(field), field=field)


def _non_negative_int(raw: Any, *, field: str) -> int:
    if isinstance(raw, bool):
        raw = None
    try:
        parsed = int(raw)
    except (TypeError, ValueError) as exc:
        raise DomainError(
            "work_reference_mapping_artifact_invalid",
            f"The reference mapping artifact requires integer {field}.",
            status=409,
            details={"field": field},
        ) from exc
    if parsed < 0:
        raise DomainError(
            "work_reference_mapping_artifact_invalid",
            f"The reference mapping artifact requires non-negative {field}.",
            status=409,
            details={"field": field},
        )
    return parsed


__all__ = [
    "MAX_REFERENCE_MAPPING_DOCUMENT_BYTES",
    "REFERENCE_MAPPING_ARTIFACT_SCHEMA",
    "REFERENCE_MAPPING_DOCUMENT_SCHEMA",
    "ReferenceMappingArtifacts",
    "decode_reference_mapping_document",
    "encode_reference_mapping_document",
]
