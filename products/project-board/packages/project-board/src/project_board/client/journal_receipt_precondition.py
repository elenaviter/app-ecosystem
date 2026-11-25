"""The one rule for advancing a journal receipt (W1031 R1), shared by the workflow guards and the store.

Preconditions:

- ``{"kind": "absent"}``: no receipt may exist (handled by the caller: the rule
  below is only consulted when a receipt exists, and then refuses).
- ``{"kind": "hash", "previous_content_hash": h, "previous_commit": c}``: the
  receipt must still hold ``h`` and, when both sides name a commit, ``c``.
- ``{"kind": "chain", "earlier": [{"commit", "content_hash"}, ...]}``: the
  receipt must be at a version published before the target. The workflow
  derives this from the publication alone, so a retry of the same publication
  is the same request (pb-knowledge, slice 2 finding 1), and an operation whose
  target is older than the receipt's version is stale (finding 3).
"""

from __future__ import annotations

from typing import Any, Mapping


def receipt_precondition_allows(existing: Mapping[str, Any] | None, precondition: Mapping[str, Any]) -> bool:
    if not existing:
        return False
    kind = str(precondition.get("kind") or "")
    current_hash = str(existing.get("content_hash") or "")
    current_commit = str(existing.get("commit") or "")
    if kind == "hash":
        if current_hash != str(precondition.get("previous_content_hash") or ""):
            return False
        previous_commit = str(precondition.get("previous_commit") or "")
        return not (previous_commit and current_commit and current_commit != previous_commit)
    if kind == "chain":
        earlier = [dict(row) for row in precondition.get("earlier") or [] if isinstance(row, Mapping)]
        if current_commit:
            return any(str(row.get("commit") or "") == current_commit for row in earlier)
        # A receipt written before W1031 has no commit: match its hash.
        return any(str(row.get("content_hash") or "") == current_hash for row in earlier)
    return False


__all__ = ["receipt_precondition_allows"]
