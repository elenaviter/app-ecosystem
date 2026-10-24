# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""A Control's account-scope policy (W941): whether it narrows the caller's provider accounts.

A Control Card governs application capability. Under AND it also intersects
the caller Card's provider accounts (``account_scope``) with its own. A Control
that declares ``account_scope_policy: "caller"`` leaves the caller's accounts
as they are: they stay the caller's own choice, still enforced by each selected
operation's live grants. This is the rule ``controls.effective`` applies to a
descriptor Control, made a property any Control may carry.

The property never adds an account; a Control without it intersects as before,
and OR is unchanged. It is written only by the Control's own transaction (the
application that manages the Control sets it there); a direct write that sets
or changes it is refused. Removing it is allowed, because that only narrows.
"""

from __future__ import annotations

from typing import Any, Mapping

ACCOUNT_SCOPE_POLICY_PROPERTY = "account_scope_policy"
ACCOUNT_SCOPE_CALLER = "caller"
ACCOUNT_SCOPE_POLICIES = frozenset({ACCOUNT_SCOPE_CALLER})
INVALID = "control_card_account_scope_policy_invalid"
REQUIRES_CONTROL_CARD = "account_scope_policy_requires_control_card"
DIRECT_WRITE_REFUSED = "account_scope_policy_requires_control_transaction"


def keeps_caller_accounts(properties: Mapping[str, Any] | None) -> bool:
    """Whether a Control with these properties leaves the caller's accounts unchanged under AND."""
    return dict(properties or {}).get(ACCOUNT_SCOPE_POLICY_PROPERTY) == ACCOUNT_SCOPE_CALLER


def valid_policy(value: Any) -> bool:
    return value in ACCOUNT_SCOPE_POLICIES


def direct_write_refusal(previous: Mapping[str, Any] | None,
                         requested: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The refusal for a direct write that sets or changes the policy; None when it keeps or removes it.

    ``requested`` is the properties map the write would store (None: the write keeps the stored map).
    """
    if requested is None or ACCOUNT_SCOPE_POLICY_PROPERTY not in requested:
        return None
    if requested[ACCOUNT_SCOPE_POLICY_PROPERTY] == dict(previous or {}).get(ACCOUNT_SCOPE_POLICY_PROPERTY):
        return None
    return {"ok": False, "error": DIRECT_WRITE_REFUSED, "status": 409, "retryable": False,
            "message": "Only the Control's own edit sets how it treats connected accounts. Nothing was saved."}


__all__ = [
    "ACCOUNT_SCOPE_CALLER", "ACCOUNT_SCOPE_POLICIES", "ACCOUNT_SCOPE_POLICY_PROPERTY", "DIRECT_WRITE_REFUSED",
    "INVALID", "REQUIRES_CONTROL_CARD", "direct_write_refusal", "keeps_caller_accounts", "valid_policy",
]
