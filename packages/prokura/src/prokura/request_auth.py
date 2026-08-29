# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Request authentication resolver.

The gateway has two request-auth boundaries:

* platform auth, implemented by the configured platform ``AuthManager``;
* Connection Hub auth, implemented by one Connection Hub authentication
  surface.

Connection Hub owns the selector for Telegram/Slack/OIDC/API-key/custom
authority authenticators. This module only asks that surface for a complete
``UserSession`` when platform auth did not already prove the request.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

SessionFactory = Callable[[Any, Any, Optional[dict[str, Any]]], Awaitable[Any]]
RequestAuthenticationSurface = Callable[[Any, Any, SessionFactory], Awaitable[Optional[Any]]]
DebugFlag = Callable[[], bool]
RoleNormalizer = Callable[[Any], Any]
UserTypeResolver = Callable[[list[str]], Any]

# ``allow_connection_hub`` mode: consult ONLY the Connection Hub surface's
# delegated platform BEARER branch (header-only by construction — it reads
# nothing but the Authorization header and the grant store). Route families
# that must never see cookie/query/provider authenticators (bundle MCP) use
# this so verified delegated automation identities still resolve.
CONNECTION_HUB_DELEGATED_BEARER_ONLY = "delegated_bearer_only"


def _debug_disabled() -> bool:
    return False


class RequestAuthResolver:
    """Boundary-level request-auth resolver.

    This resolver deliberately does not keep a provider-authenticator registry.
    External proof selection belongs to Connection Hub, so the
    gateway installs exactly one Connection Hub authentication surface.
    """

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        platform_authenticator: RequestAuthenticationSurface | None = None,
        anonymous_user_type: Any = "anonymous",
        propagated_errors: tuple[type[BaseException], ...] = (),
        debug_enabled: DebugFlag = _debug_disabled,
    ) -> None:
        self.session_factory = session_factory
        self._platform_authenticator = platform_authenticator
        self._connection_hub_surface: RequestAuthenticationSurface | None = None
        self._anonymous_user_type = anonymous_user_type
        self._propagated_errors = tuple(propagated_errors or ())
        self._debug_enabled = debug_enabled

    def install_connection_hub_surface(self, surface: RequestAuthenticationSurface) -> None:
        self._connection_hub_surface = surface

    async def resolve_session(
        self,
        request: Any,
        context: Any,
        *,
        allow_connection_hub: bool | str = True,
    ) -> Any:
        if context.authorization_header:
            session = await self._try_surface(
                self._platform_authenticator,
                request,
                context,
                label="platform",
            )
            if session is not None:
                return session

        if self._connection_hub_surface is not None:
            if allow_connection_hub is True:
                session = await self._try_surface(
                    self._connection_hub_surface,
                    request,
                    context,
                    label="connection_hub",
                )
                if session is not None:
                    return session
            elif allow_connection_hub == CONNECTION_HUB_DELEGATED_BEARER_ONLY:
                # The narrow, header-only slice of the hub surface. A surface
                # that does not expose it simply contributes nothing here —
                # unknown tokens keep resolving to the anonymous session and
                # every downstream gate stays fail-closed.
                narrow = getattr(
                    self._connection_hub_surface, "authenticate_delegated_bearer", None
                )
                if callable(narrow):
                    session = await self._try_surface(
                        narrow,
                        request,
                        context,
                        label="connection_hub.delegated_bearer",
                    )
                    if session is not None:
                        return session

        return await self.session_factory(context, self._anonymous_user_type, None)

    async def _try_surface(
        self,
        surface: RequestAuthenticationSurface | None,
        request: Any,
        context: Any,
        *,
        label: str,
    ) -> Optional[Any]:
        if surface is None:
            return None
        try:
            session = await surface(request, context, self.session_factory)
        except Exception as exc:
            if self._propagated_errors and isinstance(exc, self._propagated_errors):
                raise
            logger.warning(
                "Request-auth surface failed; continuing auth stack surface=%s",
                label,
                exc_info=self._debug_enabled(),
            )
            return None
        if session is not None and self._debug_enabled():
            logger.info(
                "Request auth resolver accepted session surface=%s user=%s type=%s",
                label,
                session.user_id,
                session.user_type.value if hasattr(session.user_type, "value") else session.user_type,
            )
        return session


class PlatformTokenAuthenticator:
    """Descriptor-registered platform token/cookie authenticator.

    This preserves the existing AuthManager implementations while exposing
    them through the same session-returning surface contract.
    """

    def __init__(
        self,
        *,
        auth_manager: Any,
        role_normalizer: RoleNormalizer,
        user_type_resolver: UserTypeResolver,
        authentication_errors: tuple[type[BaseException], ...] = (),
        debug_enabled: DebugFlag = _debug_disabled,
    ) -> None:
        self.auth_manager = auth_manager
        self._role_normalizer = role_normalizer
        self._user_type_resolver = user_type_resolver
        self._authentication_errors = tuple(authentication_errors or ())
        self._debug_enabled = debug_enabled
        self.authenticator_id = getattr(auth_manager, "authenticator_id", "") or "kdcube.platform.token"
        self.authority_id = getattr(auth_manager, "authority_id", "") or "kdcube.platform"

    async def __call__(
        self,
        _request: Any,
        context: Any,
        session_factory: SessionFactory,
    ) -> Optional[Any]:
        if not context.authorization_header or not self.auth_manager:
            if self._debug_enabled():
                logger.info(
                    "Request auth resolver: no token/auth manager auth_header=%s manager=%s",
                    bool(context.authorization_header),
                    bool(self.auth_manager),
                )
            return None

        try:
            parts = context.authorization_header.split(" ", 1)
            if len(parts) != 2 or parts[0].lower() != "bearer":
                if self._debug_enabled():
                    logger.info("Request auth resolver: malformed authorization header")
                return None

            token = parts[1]
            user = self._role_normalizer(
                await self.auth_manager.authenticate_with_both(token, context.id_token)
            )
            roles = list(getattr(user, "roles", None) or [])
            permissions = list(getattr(user, "permissions", None) or [])
            user_type = self._user_type_resolver(roles)
            user_data = {
                "user_id": getattr(user, "sub", None) or user.username,
                "username": user.username,
                "email": user.email,
                "roles": roles,
                "permissions": permissions,
                "identity_authority": {
                    "authority_id": self.authority_id,
                    "authenticator_id": self.authenticator_id,
                    "actor_user_id": getattr(user, "sub", None) or user.username,
                    "platform_user_id": getattr(user, "sub", None) or user.username,
                    "platform_roles": roles,
                    "platform_permissions": permissions,
                    "source": "platform_token_authenticator",
                },
            }
            return await session_factory(context, user_type, user_data)
        except Exception as exc:
            if self._authentication_errors and isinstance(
                exc, self._authentication_errors
            ):
                if self._debug_enabled():
                    logger.info("Request auth resolver: token rejected: %s", exc)
                return None
            logger.warning(
                "Request auth resolver: unexpected platform auth failure: %s: %s",
                type(exc).__name__,
                str(exc),
                exc_info=self._debug_enabled(),
            )
            return None


__all__ = [
    "PlatformTokenAuthenticator",
    "RequestAuthenticationSurface",
    "RequestAuthResolver",
    "SessionFactory",
    "DebugFlag",
    "RoleNormalizer",
    "UserTypeResolver",
]
