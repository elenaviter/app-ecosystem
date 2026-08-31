from __future__ import annotations

from types import SimpleNamespace

import pytest

from connection_hub.request_auth import (
    CONNECTION_HUB_DELEGATED_BEARER_ONLY,
    RequestAuthResolver,
)


async def session_factory(
    _context: object,
    user_type: object,
    user_data: dict | None,
) -> object:
    return SimpleNamespace(
        user_type=user_type,
        user_id=(user_data or {}).get("user_id", "anonymous"),
    )


@pytest.mark.asyncio
async def test_platform_authentication_precedes_the_hub_surface() -> None:
    calls: list[str] = []

    async def platform(_request: object, _context: object, _factory: object) -> object:
        calls.append("platform")
        return SimpleNamespace(user_id="platform-user")

    async def hub(_request: object, _context: object, _factory: object) -> object:
        calls.append("hub")
        return SimpleNamespace(user_id="hub-user")

    resolver = RequestAuthResolver(
        session_factory=session_factory,
        platform_authenticator=platform,
        anonymous_user_type="anonymous",
    )
    resolver.install_connection_hub_surface(hub)
    context = SimpleNamespace(authorization_header="Bearer token")

    session = await resolver.resolve_session(object(), context)

    assert session.user_id == "platform-user"
    assert calls == ["platform"]


@pytest.mark.asyncio
async def test_delegated_bearer_mode_calls_only_the_narrow_hub_branch() -> None:
    calls: list[str] = []

    class Hub:
        async def __call__(
            self,
            _request: object,
            _context: object,
            _factory: object,
        ) -> object:
            raise AssertionError("the broad hub surface must stay closed")

        async def authenticate_delegated_bearer(
            self,
            _request: object,
            _context: object,
            _factory: object,
        ) -> object:
            calls.append("delegated")
            return SimpleNamespace(user_id="automation-user")

    resolver = RequestAuthResolver(
        session_factory=session_factory,
        anonymous_user_type="anonymous",
    )
    resolver.install_connection_hub_surface(Hub())

    session = await resolver.resolve_session(
        object(),
        SimpleNamespace(authorization_header="Bearer delegated"),
        allow_connection_hub=CONNECTION_HUB_DELEGATED_BEARER_ONLY,
    )

    assert session.user_id == "automation-user"
    assert calls == ["delegated"]


@pytest.mark.asyncio
async def test_declared_authorization_errors_cross_the_boundary() -> None:
    class Denied(RuntimeError):
        pass

    async def hub(_request: object, _context: object, _factory: object) -> None:
        raise Denied("denied")

    resolver = RequestAuthResolver(
        session_factory=session_factory,
        anonymous_user_type="anonymous",
        propagated_errors=(Denied,),
    )
    resolver.install_connection_hub_surface(hub)

    with pytest.raises(Denied):
        await resolver.resolve_session(
            object(),
            SimpleNamespace(authorization_header="Bearer delegated"),
        )
