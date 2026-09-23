from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..contract.errors import DomainError
from .io import bounded_text


MAIL_ATTACHMENT_SCHEMA = "problem-board.local-mail-attachment.v1"
MAIL_ATTACHMENT_NOTICE_SCHEMA = "problem-board.worker-attachment-notice.v1"
WORKER_ATTACHMENT_READ_SCHEMA = "problem-board.worker-attachment-read.v1"


def safe_attachment_filename(value: Any) -> str:
    """Return one basename that cannot escape an attachment directory."""

    supplied = bounded_text(
        value,
        field="attachment.filename",
        maximum=512,
        required=True,
    ).replace("\\", "/")
    filename = supplied.rsplit("/", 1)[-1]
    if filename in {"", ".", ".."}:
        raise DomainError(
            "field_attachment_filename_invalid",
            "An attachment filename must name one file.",
            details={"filename": supplied},
        )
    return filename


def _attachment_size(value: Any) -> int:
    if isinstance(value, bool):
        raise DomainError(
            "field_attachment_size_invalid",
            "An attachment size must be a non-negative integer.",
        )
    try:
        size = int(value or 0)
    except (TypeError, ValueError) as exc:
        raise DomainError(
            "field_attachment_size_invalid",
            "An attachment size must be a non-negative integer.",
        ) from exc
    if size < 0:
        raise DomainError(
            "field_attachment_size_invalid",
            "An attachment size must be a non-negative integer.",
        )
    return size


def normalize_attachment_manifest(
    values: Sequence[Mapping[str, Any]] | None,
    *,
    require_local_path: bool,
) -> list[dict[str, Any]]:
    """Normalize the durable attachment fields and reject ambiguous entries."""

    manifest: list[dict[str, Any]] = []
    seen_refs: set[str] = set()
    for index, value in enumerate(values or []):
        if not isinstance(value, Mapping):
            raise DomainError(
                "field_attachment_descriptor_invalid",
                "Every attachment descriptor must be a JSON object.",
                details={"index": index, "value_type": type(value).__name__},
            )
        file_ref = bounded_text(
            value.get("file_ref"),
            field="attachment.file_ref",
            maximum=2000,
            required=True,
        )
        if file_ref in seen_refs:
            raise DomainError(
                "field_attachment_ref_duplicate",
                "Every attachment in one message must have a distinct file_ref.",
                details={"file_ref": file_ref},
            )
        seen_refs.add(file_ref)
        row = {
            "schema": MAIL_ATTACHMENT_SCHEMA,
            "filename": safe_attachment_filename(value.get("filename")),
            "mime": bounded_text(
                value.get("mime") or "application/octet-stream",
                field="attachment.mime",
                maximum=256,
                required=True,
            ),
            "size": _attachment_size(value.get("size")),
            "file_ref": file_ref,
        }
        local_path = bounded_text(
            value.get("local_path"),
            field="attachment.local_path",
            maximum=8192,
            required=require_local_path,
        )
        if local_path:
            row["local_path"] = local_path
        sha256 = bounded_text(
            value.get("sha256"),
            field="attachment.sha256",
            maximum=64,
        ).lower()
        if sha256:
            if len(sha256) != 64 or any(
                character not in "0123456789abcdef" for character in sha256
            ):
                raise DomainError(
                    "field_attachment_hash_invalid",
                    "An attachment sha256 must be 64 lowercase hexadecimal characters.",
                    details={"file_ref": file_ref},
                )
            row["sha256"] = sha256
        manifest.append(row)
    return manifest


def stored_attachment_manifest(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Read current top-level descriptors or promote the legacy payload shape."""

    raw = message.get("attachments")
    if raw is None:
        payload = (
            message.get("payload")
            if isinstance(message.get("payload"), Mapping)
            else {}
        )
        raw = payload.get("attachments")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise DomainError(
            "field_mail_attachments_invalid",
            "A mail attachment manifest must be a JSON array.",
            details={"value_type": type(raw).__name__},
        )
    return normalize_attachment_manifest(raw, require_local_path=True)


def worker_message_with_attachments(
    message: Mapping[str, Any],
    *,
    project_ref: str,
    attachment_root: Path,
    runtime_kind: str = "",
    runtime_session_id: str = "",
) -> dict[str, Any]:
    """Return the one model-facing mail shape used by receive and lease-read."""

    projected = dict(message)
    stored = stored_attachment_manifest(message)
    lease = message.get("lease") if isinstance(message.get("lease"), Mapping) else {}
    message_ref = str(message.get("message_ref") or "")
    lease_id = str(lease.get("lease_id") or "")
    visible: list[dict[str, Any]] = []
    for item in stored:
        item = verified_attachment(
            item,
            attachment_root=attachment_root,
            message_ref=message_ref,
            verify_hash=False,
        )
        command = ["pb", "worker", "attachment-read"]
        if runtime_kind == "claude-code":
            command.extend(
                [
                    "--runtime-kind",
                    runtime_kind,
                    "--runtime-session-id",
                    runtime_session_id,
                ]
            )
        if project_ref:
            command.extend(["--project-ref", project_ref])
        command.extend(
            [
                "--message-ref",
                message_ref,
                "--lease-id",
                lease_id,
                "--file-ref",
                str(item["file_ref"]),
            ]
        )
        row = {
            key: item[key]
            for key in ("schema", "filename", "mime", "size", "file_ref", "sha256")
            if key in item
        }
        row["read_command"] = command
        visible.append(row)

    payload = (
        dict(message.get("payload") or {})
        if isinstance(message.get("payload"), Mapping)
        else {}
    )
    payload.pop("attachments", None)
    if isinstance(payload.get("command"), Mapping):
        command_payload = dict(payload["command"])
        command_payload.pop("attachments", None)
        payload["command"] = command_payload
    projected["payload"] = payload
    projected["attachment_count"] = len(visible)
    projected["attachments"] = visible
    if visible:
        noun = "attachment" if len(visible) == 1 else "attachments"
        projected["attachment_notice"] = {
            "schema": MAIL_ATTACHMENT_NOTICE_SCHEMA,
            "count": len(visible),
            "message": (
                f"This message includes {len(visible)} {noun}. Read every "
                "attachment before handling or settling this message."
            ),
        }
    else:
        projected.pop("attachment_notice", None)
    return projected


def attachment_by_ref(
    message: Mapping[str, Any], *, file_ref: str
) -> dict[str, Any]:
    clean_ref = bounded_text(
        file_ref,
        field="file_ref",
        maximum=2000,
        required=True,
    )
    matches = [
        item
        for item in stored_attachment_manifest(message)
        if str(item.get("file_ref") or "") == clean_ref
    ]
    if not matches:
        raise DomainError(
            "field_mail_attachment_not_found",
            "The active mail lease does not contain the requested attachment.",
            status=404,
            details={
                "message_ref": str(message.get("message_ref") or ""),
                "file_ref": clean_ref,
            },
        )
    if len(matches) != 1:
        raise DomainError(
            "field_mail_attachment_ambiguous",
            "The active mail lease contains the same file_ref more than once.",
            status=409,
            details={"file_ref": clean_ref},
        )
    return matches[0]


def verified_attachment(
    attachment: Mapping[str, Any],
    *,
    attachment_root: Path,
    message_ref: str,
    verify_hash: bool,
) -> dict[str, Any]:
    """Validate one attachment locator and its immutable byte metadata."""

    path = validated_attachment_path(
        attachment.get("local_path"),
        attachment_root=attachment_root,
    )
    actual_size = path.stat().st_size
    expected_size = int(attachment.get("size") or 0)
    actual_hash = file_sha256(path) if verify_hash else ""
    expected_hash = str(attachment.get("sha256") or "")
    if expected_size != actual_size or (
        verify_hash and expected_hash and expected_hash != actual_hash
    ):
        details = {
            "message_ref": message_ref,
            "file_ref": str(attachment.get("file_ref") or ""),
            "expected_size": expected_size,
            "actual_size": actual_size,
        }
        if verify_hash:
            details.update(
                expected_sha256=expected_hash,
                actual_sha256=actual_hash,
            )
        raise DomainError(
            "field_mail_attachment_integrity_mismatch",
            "The local attachment bytes no longer match the delivered manifest.",
            status=409,
            details=details,
        )
    verified = dict(attachment)
    verified["local_path"] = str(path)
    verified["size"] = actual_size
    if verify_hash:
        verified["sha256"] = actual_hash
    return verified


def materialized_attachment_manifest(
    values: Sequence[Mapping[str, Any]],
    *,
    local_paths: Mapping[str, Any],
    local_files: Mapping[str, Any],
    attachment_root: Path,
) -> list[dict[str, Any]]:
    """Bind signed remote descriptors to downloaded, contained local bytes."""

    materialized: list[dict[str, Any]] = []
    for index, value in enumerate(values):
        if not isinstance(value, Mapping):
            raise DomainError(
                "field_control_attachment_invalid",
                "Every control attachment must be a JSON object.",
                details={"index": index, "value_type": type(value).__name__},
            )
        file_ref = str(value.get("file_ref") or "")
        filename = str(value.get("filename") or "")
        local_file = (
            dict(local_files.get(file_ref) or {})
            if isinstance(local_files.get(file_ref), Mapping)
            else {}
        )
        local_path = (
            local_file.get("local_path")
            or local_paths.get(file_ref)
            or local_paths.get(filename)
        )
        if not local_path:
            raise DomainError(
                "field_control_attachment_unavailable",
                "A control attachment was not downloaded into this worker mailbox.",
                status=409,
                details={
                    "index": index,
                    "file_ref": file_ref,
                    "filename": filename,
                },
            )
        normalized = normalize_attachment_manifest(
            [{**dict(value), "local_path": str(local_path)}],
            require_local_path=True,
        )[0]
        verified = verified_attachment(
            normalized,
            attachment_root=attachment_root,
            message_ref="",
            verify_hash=False,
        )
        verified["sha256"] = file_sha256(Path(verified["local_path"]))
        materialized.append(verified)
    return normalize_attachment_manifest(
        materialized,
        require_local_path=True,
    )


def validated_attachment_path(value: Any, *, attachment_root: Path) -> Path:
    """Resolve one stored path and prove it stays inside this mailbox."""

    supplied = bounded_text(
        value,
        field="attachment.local_path",
        maximum=8192,
        required=True,
    )
    try:
        root = attachment_root.resolve(strict=True)
        path = Path(supplied).expanduser().resolve(strict=True)
        path.relative_to(root)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise DomainError(
            "field_mail_attachment_unavailable",
            "The attachment is not a readable file inside this worker mailbox.",
            status=409,
            details={"local_path": supplied},
        ) from exc
    if not path.is_file() or not os.access(path, os.R_OK):
        raise DomainError(
            "field_mail_attachment_unavailable",
            "The attachment is not a readable file inside this worker mailbox.",
            status=409,
            details={"local_path": supplied},
        )
    return path


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
