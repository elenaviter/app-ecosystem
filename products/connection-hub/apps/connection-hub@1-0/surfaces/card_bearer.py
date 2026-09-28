"""An agent's Card bearer, authenticated for identity only (W371).

``project_agent_github_token_issue`` gives an attending agent its owner's
GitHub token. The authority for it is the project host's
``project_agent_github_authorize`` (attendance, the Card's "Use GitHub", the
repository on the project card); this route only needs to know which live
Card is asking and whose it is.

The Card bearer therefore arrives in its own header, not ``Authorization``.
The platform authorizes every application operation called with a delegated
``Authorization`` bearer against the Card's selected application operations,
and no catalog offers this route, so no Card could hold it: the first real push
(2026-09-28, 15:48Z) was refused with ``delegated_capability_not_granted``
before the route ran. With the bearer in this header, the platform sees no
delegated bearer, and the route itself verifies it: the same bearer
verification, live-Card restoration and issuer check as the delegated proxy
guard, with no resource or operation match.
"""

from __future__ import annotations

import logging
from typing import Any

from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.oauth import (
    surface_guard,
)

CARD_BEARER_HEADER = "x-connection-hub-card-bearer"
LOGGER = logging.getLogger("kdcube.connection_hub.card_bearer")


def card_bearer(request: Any) -> str:
    """The Card bearer from its header; a leading ``Bearer`` is accepted."""

    headers = getattr(request, "headers", None) or {}
    value = str(headers.get(CARD_BEARER_HEADER) or "").strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    return value


async def authenticate_card_bearer(request: Any, token: str) -> Any:
    """Verify a live Card bearer for identity; a denial response, else None.

    On success the request carries the delegated credential facts that
    ``DelegatedCredentialView.from_request`` reads (grantor, Card access id,
    client id).
    """

    denial, _user, _envelope, _grant_record = await surface_guard._authorize_delegated_managed_request(  # noqa: SLF001
        request=request,
        auth=None,
        authority_id="delegated_client",
        roles=(),
        permissions=(),
        logger=LOGGER,
        surface_label="card_bearer",
        token=token,
        request_resource="*",
        match_request_resource=False,
    )
    return denial


def forget_card_bearer(request: Any) -> None:
    """Drop the agent's delegated credential from the request once its facts are read.

    Connection Hub then asks the project host as itself: the peer call runs
    inside this request, and a request carrying the agent's Card made the
    platform hold that call to the agent Card's selected application
    operations (``authorize_delegated_application_operation_request``), which
    found no bearer and refused it, before the board saw it (second blocker of
    the first real push, 2026-09-28, 16:01Z). The peer proof in the body is
    that call's only admission.
    """

    state = getattr(request, "state", None)
    if state is None:
        return
    try:
        state.delegated_credential = None
    except Exception:  # noqa: BLE001 - a request state that refuses the reset keeps nothing to forget
        pass
    surface_guard._store_live_card_composition(request, None)  # noqa: SLF001


__all__ = ["CARD_BEARER_HEADER", "authenticate_card_bearer", "card_bearer", "forget_card_bearer"]
