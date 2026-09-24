"""Whether a failure means the credential itself was refused.

One predicate for every caller that has to tell a dead credential from a
server that is failing. ``pb worker authorize`` uses it to decide on a browser
reconnect, and the relay records it with each refusal, so a relay restart
parks a dead credential and retries everything else (W292).

A code alone is not enough: ``oauth_token_request_failed`` covers a token
endpoint that answered ``invalid_grant`` and one that was down. On 2026-09-24
the same code carried a 503 (``token withheld: delegated card conflict``)
and then the 400 ``invalid_grant``. Only the second is the credential's answer.
"""

from __future__ import annotations

from collections.abc import Mapping

RECONNECT_CODES = frozenset(
    {
        "credential_missing",
        "delegated_card_refresh_refused",
        "mcp_authorization_rejected",
        "oauth_profile_credential_missing",
        "oauth_profile_login_required",
        "oauth_profile_access_id_mismatch",
        "oauth_profile_credential_invalid",
        "oauth_refresh_unsupported",
    }
)
# A Card revoked or deleted on the server: only a new authorization answers it.
REVOKED_CARD_CODES = frozenset({"delegated_card_revoked", "delegated_card_not_found"})


def requires_browser_reconnect(exc: BaseException) -> bool:
    """The credential must be authorized again in a browser."""

    code = str(getattr(exc, "code", "") or type(exc).__name__)
    if code in RECONNECT_CODES:
        return True
    if code != "oauth_token_request_failed":
        return False
    details = getattr(exc, "details", {})
    return isinstance(details, Mapping) and details.get("oauth_error") == "invalid_grant"


def credential_refused(error: BaseException) -> bool:
    """Whether ``error``, or anything it wraps, is the credential's own refusal."""

    seen: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in seen and len(seen) < 8:
        seen.append(current)
        if requires_browser_reconnect(current):
            return True
        if str(getattr(current, "code", "") or "") in REVOKED_CARD_CODES:
            return True
        current = current.__cause__ or current.__context__
    return False


__all__ = [
    "RECONNECT_CODES",
    "REVOKED_CARD_CODES",
    "credential_refused",
    "requires_browser_reconnect",
]
