from __future__ import annotations

from typing import Any, Mapping

from ..contract.errors import DomainError


JOURNAL_RECEIPT_CONFLICT_CODE = "field_journal_receipt_conflict"
JOURNAL_UPDATE_STALE_CODE = "journal_update_stale"
SUPERSEDING_ENTRY_ACTION = "author_superseding_journal_entry"
UPDATE_ENTRY_ACTION = "rerun_journal_index_with_update"


def observe_receipt_identity(
    entry: Mapping[str, Any], receipt: Mapping[str, Any] | None
) -> dict[str, Any]:
    expected_entry_ref = str(entry.get("entry_ref") or "")
    expected_hash = str(entry.get("content_hash") or "")
    expected_repository_ref = str(entry.get("repository_journal_ref") or "")
    if receipt is None:
        return {
            "state": "absent",
            "matches": False,
            "entry_ref": expected_entry_ref,
            "expected_entry_ref": expected_entry_ref,
            "expected_content_hash": expected_hash,
            "expected_repository_journal_ref": expected_repository_ref,
        }

    observed = dict(receipt)
    observed_entry_ref = str(observed.get("entry_ref") or "")
    observed_hash = str(observed.get("content_hash") or "")
    observed_repository_ref = str(observed.get("repository_journal_ref") or "")
    matches = (
        observed_entry_ref == expected_entry_ref
        and observed_hash == expected_hash
        and observed_repository_ref == expected_repository_ref
    )
    return {
        "state": "matched" if matches else "identity_conflict",
        "matches": matches,
        "entry_ref": observed_entry_ref,
        "expected_entry_ref": expected_entry_ref,
        "content_hash": observed_hash,
        "expected_content_hash": expected_hash,
        "repository_journal_ref": observed_repository_ref,
        "expected_repository_journal_ref": expected_repository_ref,
    }


def has_receipt_identity_conflict(observation: Mapping[str, Any]) -> bool:
    return str(observation.get("state") or "") == "identity_conflict"


def superseding_entry_action(observation: Mapping[str, Any]) -> dict[str, Any]:
    entry_ref = str(
        observation.get("entry_ref")
        or observation.get("expected_entry_ref")
        or ""
    )
    return {
        "code": SUPERSEDING_ENTRY_ACTION,
        "message": (
            "Author a new journal entry with a new unique entry_ref and set "
            "supersedes_ref to the existing entry_ref."
        ),
        "supersedes_ref": entry_ref,
    }


def update_entry_action(observation: Mapping[str, Any]) -> dict[str, Any]:
    """W1031: the second route for an amended entry, as an exact rerun."""

    repository_ref = str(
        observation.get("expected_repository_journal_ref")
        or observation.get("repository_journal_ref")
        or ""
    )
    return {
        "code": UPDATE_ENTRY_ACTION,
        "message": (
            "The entry was amended under the same entry_ref: rerun journal-index "
            "with --update to index the amended content deliberately."
        ),
        "command": (
            "pb worker journal-index --project-ref <project-ref> "
            f"--repository-journal-ref {repository_ref or '<repo:alias/path>'} --update"
        ),
    }


def update_stale_error(
    observation: Mapping[str, Any],
    *,
    previous_content_hash: str,
    operation_id: str = "",
) -> DomainError:
    """W1031 R1: an update whose precondition no longer holds never overwrites a newer receipt."""

    details = {
        "entry_ref": str(observation.get("expected_entry_ref") or observation.get("entry_ref") or ""),
        "expected_previous_content_hash": str(previous_content_hash or ""),
        "recorded_content_hash": str(observation.get("content_hash") or ""),
        "target_content_hash": str(observation.get("expected_content_hash") or ""),
    }
    if operation_id:
        details["operation_id"] = operation_id
    return DomainError(
        JOURNAL_UPDATE_STALE_CODE,
        (
            "The receipt changed since this update was prepared. Fetch and fast-forward "
            "the journal clone, then run journal-index --update again."
        ),
        status=409,
        details=details,
    )


def receipt_identity_conflict_error(
    observation: Mapping[str, Any], *, operation_id: str = ""
) -> DomainError:
    details = {
        "entry_ref": str(
            observation.get("expected_entry_ref")
            or observation.get("entry_ref")
            or ""
        ),
        "recorded_entry_ref": str(observation.get("entry_ref") or ""),
        "expected_content_hash": str(
            observation.get("expected_content_hash") or ""
        ),
        "recorded_content_hash": str(observation.get("content_hash") or ""),
        "expected_repository_journal_ref": str(
            observation.get("expected_repository_journal_ref") or ""
        ),
        "recorded_repository_journal_ref": str(
            observation.get("repository_journal_ref") or ""
        ),
        # Kept for readers of the W207 contract; update_action is the W1031 second route.
        "required_action": superseding_entry_action(observation),
        "update_action": update_entry_action(observation),
    }
    if operation_id:
        details["operation_id"] = operation_id
    return DomainError(
        JOURNAL_RECEIPT_CONFLICT_CODE,
        (
            "This journal entry identity already records different content. "
            "For an amended entry, rerun journal-index with --update; for a real "
            "replacement, create a new entry_ref that supersedes the existing entry."
        ),
        status=409,
        details=details,
    )


__all__ = [
    "JOURNAL_RECEIPT_CONFLICT_CODE",
    "JOURNAL_UPDATE_STALE_CODE",
    "SUPERSEDING_ENTRY_ACTION",
    "UPDATE_ENTRY_ACTION",
    "has_receipt_identity_conflict",
    "observe_receipt_identity",
    "receipt_identity_conflict_error",
    "superseding_entry_action",
    "update_entry_action",
    "update_stale_error",
]
