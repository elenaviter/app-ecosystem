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


@pytest.mark.asyncio
@pytest.mark.parametrize(("verified", "carried"), [(True, True), (False, False), (None, None)])
async def test_the_platform_token_authenticator_carries_the_email_verdict_when_known(verified, carried) -> None:
    """W260: the session's user_data carried every identity field but the email verdict.

    On 2026-09-26 an invited LinkedIn user's bundle record held
    email_verified true while the app session stayed null, because this
    authenticator built user_data without the key. A known verdict is carried;
    an unknown one leaves the key out, so the presence-based session merge
    keeps what it had.
    """

    from connection_hub.request_auth import PlatformTokenAuthenticator

    class Manager:
        async def authenticate_with_both(self, token, id_token):
            return SimpleNamespace(
                sub="cognito:user-1", username="person@example.test", email="person@example.test",
                email_verified=verified, roles=["kdcube:role:registered"], permissions=[],
            )

    seen: list[dict] = []

    async def capture(_context, _user_type, user_data):
        seen.append(dict(user_data))
        return SimpleNamespace(user_data=user_data)

    authenticator = PlatformTokenAuthenticator(
        auth_manager=Manager(),
        role_normalizer=lambda user: user,
        user_type_resolver=lambda roles: "registered",
    )
    context = SimpleNamespace(authorization_header="Bearer kst1.token", id_token=None)
    assert await authenticator(None, context, capture) is not None
    [user_data] = seen
    assert user_data.get("email_verified", None) is carried
    assert ("email_verified" in user_data) is (carried is not None)



@pytest.mark.asyncio
@pytest.mark.parametrize(("session_id", "carried"), [("bsn_1", "bsn_1"), (None, None), ("  ", None)])
async def test_the_platform_token_authenticator_names_the_sign_in_it_authenticated(session_id, carried) -> None:
    """W260: the app session is rebuilt when a request comes from another sign-in."""

    from connection_hub.request_auth import PlatformTokenAuthenticator

    class Manager:
        async def authenticate_with_both(self, token, id_token):
            return SimpleNamespace(
                sub="cognito:user-1", username="u", email="u@example.test", email_verified=True,
                roles=[], permissions=[], session_id=session_id,
            )

    seen: list[dict] = []

    async def capture(_context, _user_type, user_data):
        seen.append(dict(user_data))
        return SimpleNamespace()

    authenticator = PlatformTokenAuthenticator(
        auth_manager=Manager(), role_normalizer=lambda user: user, user_type_resolver=lambda roles: "registered",
    )
    await authenticator(None, SimpleNamespace(authorization_header="Bearer kst1.t", id_token=None), capture)
    [user_data] = seen
    assert user_data.get("platform_session_id") == carried
