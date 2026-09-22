"""Open the Card's Data Bus session, and re-mint the bearer once when the server refuses it.

A profile's bearer is a session the card minted, and the session lives
server-side. The client refreshes it on its own clock, so a server that lost
the session behind a locally current token (a store restart on 2026-09-21
took three of four relays out for an hour) answers 401 to a bearer this side
still trusts, and the relay re-presents the same dead bearer every cycle.

Here a refused admission with an OAuth-backed profile triggers exactly one
refresh through the card's refresh token, and the connection is opened again
with what the refresh returned. What the refresh says is then the card's
answer: a grant refused by the token endpoint (HTTP 400 to 403) is permanent
and named as such, a token endpoint that could not be reached is this
cycle's transport, and a second refusal after a good refresh is the refusal
the relay always classified, unchanged.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from ..contract.errors import DomainError

logger = logging.getLogger("problem_board.relay.admission")

# The token endpoint refused the grant: the card is revoked, expired, or was
# never this client's. Asking again with the same refresh token cannot change it.
REFRESH_REFUSED_STATUSES = frozenset({400, 401, 403})
REFRESH_REFUSED_CODE = "delegated_card_refresh_refused"
REFRESH_LOGIN_REQUIRED_CODES = frozenset({"oauth_profile_login_required", "oauth_refresh_unsupported"})


def is_admission_refusal(error: BaseException) -> bool:
    """True when the server admitted the transport and refused this credential."""

    try:
        from app_foundation.data_bus import DataBusIngressRejected
    except Exception:  # pragma: no cover - transport dependency absent
        return False
    return isinstance(error, DataBusIngressRejected)


def is_namespace_handshake_timeout(error: BaseException) -> bool:
    """True when the server accepted the connection and only the namespace
    handshake timed out.

    python-socketio raises the same "One or more namespaces failed to connect"
    whether the server refused the namespace or never answered it. The Data
    Bus client turns a refusal (a connect_error from the server) into
    DataBusIngressRejected, so a raw socketio failure with that message and no
    refusal anywhere in its chain is a handshake that did not finish in time.
    That is a load symptom worth retrying soon, not a refusal (W265).
    """

    seen: list[BaseException] = []
    current: BaseException | None = error
    timed_out = False
    while current is not None and current not in seen and len(seen) < 8:
        seen.append(current)
        if is_admission_refusal(current):
            return False
        if str(type(current).__module__ or "").startswith("socketio") and (
            "namespaces failed to connect" in str(current).lower()
        ):
            timed_out = True
        current = current.__cause__ or current.__context__
    return timed_out


def refresh_was_refused(error: BaseException) -> bool:
    """True when the token endpoint answered the refresh with a refusal rather than an outage."""

    code = str(getattr(error, "code", "") or "")
    if code in REFRESH_LOGIN_REQUIRED_CODES:
        return True
    status = getattr(error, "status", None)
    if status is None:
        details = getattr(error, "details", None)
        status = details.get("status") if isinstance(details, dict) else None
    try:
        return int(status or 0) in REFRESH_REFUSED_STATUSES
    except (TypeError, ValueError):
        return False


def _oauth_error(error: BaseException) -> str:
    """The token endpoint's registered error code, when the transport carried one."""

    details = getattr(error, "details", None)
    return str(details.get("oauth_error") or "") if isinstance(details, dict) else ""


async def open_with_one_refresh(
    *,
    open_bus: Callable[[str], Awaitable[Any]],
    bearer: str,
    refresh_bearer: Optional[Callable[[], Awaitable[str]]],
    profile: str,
    target: str,
) -> tuple[Any, dict[str, Any]]:
    """Open the bus with ``bearer``, and on a refused admission refresh once and open again.

    ``open_bus(bearer)`` builds and connects a client and returns it, or raises.
    ``refresh_bearer()`` mints a new bearer through the card, or is None for a
    profile that cannot refresh (a static bearer), in which case the refusal
    is raised as it came. Returns the open client and a receipt naming what
    happened, for the relay's log and the worker's diagnostics.
    """

    receipt: dict[str, Any] = {"profile": profile, "refreshed": False, "refusals": 0}
    try:
        return await open_bus(bearer), receipt
    except Exception as first:
        if not is_admission_refusal(first) or refresh_bearer is None:
            raise
        receipt["refusals"] = 1
        receipt["first_refusal"] = {
            "code": str(getattr(first, "code", "") or ""),
            "message": str(first)[:200],
        }
        logger.warning(
            "[relay.admission] refused profile=%s target=%s code=%s: refreshing the Card session once",
            profile,
            target,
            receipt["first_refusal"]["code"] or "unnamed",
        )
        try:
            replacement = await refresh_bearer()
        except Exception as refresh_error:
            if refresh_was_refused(refresh_error):
                oauth_error = _oauth_error(refresh_error)
                logger.error(
                    "[relay.admission] refresh refused profile=%s code=%s status=%s oauth_error=%s",
                    profile,
                    getattr(refresh_error, "code", ""),
                    getattr(refresh_error, "status", None),
                    oauth_error or "unnamed",
                )
                refused = DomainError(
                    REFRESH_REFUSED_CODE,
                    "The Data Bus refused the Card bearer and the token endpoint refused to mint a new one "
                    f"({oauth_error or 'no error code given'}). "
                    + (
                        "invalid_grant: the server no longer holds this grant, so only a new "
                        "authorization of this profile mints another."
                        if oauth_error == "invalid_grant"
                        else "Read the code before re-authorizing: only invalid_grant means the grant is gone."
                    ),
                    status=401,
                    details={
                        "profile": profile,
                        "admission": receipt["first_refusal"],
                        "refresh_code": str(getattr(refresh_error, "code", "") or ""),
                        "refresh_status": getattr(refresh_error, "status", None),
                        "oauth_error": oauth_error,
                        "retryable": False,
                    },
                )
                raise refused from refresh_error
            # The token endpoint could not be reached: this cycle's transport,
            # and the next cycle tries the whole thing again.
            raise refresh_error from first
        receipt["refreshed"] = True
        logger.info(
            "[relay.admission] refreshed profile=%s: opening the Data Bus again with the new bearer",
            profile,
        )
        try:
            return await open_bus(replacement), receipt
        except Exception as second:
            if is_admission_refusal(second):
                receipt["refusals"] = 2
                logger.error(
                    "[relay.admission] refused again after a good refresh profile=%s code=%s: not a stale session",
                    profile,
                    getattr(second, "code", "") or "unnamed",
                )
            raise
