from __future__ import annotations

from types import SimpleNamespace

import pytest

from prokura.federated_tokens.data_bus import (
    FederatedTokenInvalid,
    issue_federated_data_bus_token,
    verify_federated_data_bus_token,
)


class MemoryRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def setex(self, key: str, _ttl: int, value: str) -> None:
        self.values[key] = value

    async def get(self, key: str) -> str | None:
        return self.values.get(key)


class SessionManager:
    def __init__(self) -> None:
        self.sessions: dict[str, object] = {}
        self.redis = None

    async def init_redis(self) -> None:
        return None

    async def get_or_create_session(
        self,
        context: object,
        user_type: object,
        user_data: dict,
    ) -> object:
        session = SimpleNamespace(
            session_id=f"session-{user_data['user_id']}",
            user_id=user_data["user_id"],
            user_type=user_type,
            request_context=context,
        )
        self.sessions[session.session_id] = session
        return session

    async def get_session_by_id(self, session_id: str) -> object | None:
        return self.sessions.get(session_id)


@pytest.mark.asyncio
async def test_issue_and_verify_a_transport_scoped_token() -> None:
    redis = MemoryRedis()
    sessions = SessionManager()

    grant = await issue_federated_data_bus_token(
        session_manager=sessions,
        request_context={"client": "test"},
        redis=redis,
        tenant="tenant-a",
        project="project-a",
        bundle_id="app@1-0",
        user_id="user-1",
        user_type="registered",
        secret="test-secret",
    )
    verified = await verify_federated_data_bus_token(
        token=grant.token,
        tenant="tenant-a",
        project="project-a",
        bundle_id="app@1-0",
        redis=redis,
        session_manager=sessions,
        secret="test-secret",
    )

    assert verified.session.session_id == grant.session.session_id
    assert verified.claims["allowed_transports"] == ["data_bus"]
    assert verified.claims["credential"]["credential_kind"] == "derived_session"


@pytest.mark.asyncio
async def test_token_is_bound_to_the_issuing_application() -> None:
    redis = MemoryRedis()
    sessions = SessionManager()
    grant = await issue_federated_data_bus_token(
        session_manager=sessions,
        request_context={},
        redis=redis,
        tenant="tenant-a",
        project="project-a",
        bundle_id="app@1-0",
        user_id="user-1",
        secret="test-secret",
    )

    with pytest.raises(FederatedTokenInvalid):
        await verify_federated_data_bus_token(
            token=grant.token,
            tenant="tenant-a",
            project="project-a",
            bundle_id="other@1-0",
            redis=redis,
            session_manager=sessions,
            secret="test-secret",
        )
