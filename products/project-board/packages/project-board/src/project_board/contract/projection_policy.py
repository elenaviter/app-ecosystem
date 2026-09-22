"""Shared bounds and field policy for the public Project Board projection."""

from __future__ import annotations


PLAN_PROJECTION_PAGE_LIMIT = 50
FORBIDDEN_KEYS = {
    "acceptance",
    "body",
    "content",
    "credential",
    "description",
    "evidence",
    "goal",
    "journal",
    "mail",
    "payload",
    "secret",
    "source_path",
    "token",
}


__all__ = ["FORBIDDEN_KEYS", "PLAN_PROJECTION_PAGE_LIMIT"]
