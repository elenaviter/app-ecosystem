# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Canonical durable representation of an OAuth client record."""

from __future__ import annotations

from typing import Any, Mapping


OAUTH_CLIENT_RECORD_FIELDS = frozenset(
    {
        "application_type",
        "client_id",
        "grant_types",
        "metadata",
        "redirect_uris",
        "token_endpoint_auth_method",
    }
)


def canonical_oauth_client_record(value: Mapping[str, Any]) -> dict[str, Any]:
    """Apply durable defaults and reject fields the authority cannot retain."""

    payload = dict(value or {})
    unsupported = sorted(set(payload).difference(OAUTH_CLIENT_RECORD_FIELDS))
    if unsupported:
        raise ValueError(
            "OAuth client record contains unsupported fields: "
            + ",".join(unsupported)
        )
    return {
        "client_id": str(payload.get("client_id") or "").strip(),
        "redirect_uris": list(payload.get("redirect_uris") or []),
        "grant_types": list(
            payload.get("grant_types")
            or ("authorization_code", "refresh_token")
        ),
        "token_endpoint_auth_method": str(
            payload.get("token_endpoint_auth_method") or "none"
        ),
        "application_type": str(payload.get("application_type") or "native"),
        "metadata": dict(payload.get("metadata") or {}),
    }


__all__ = ["OAUTH_CLIENT_RECORD_FIELDS", "canonical_oauth_client_record"]
