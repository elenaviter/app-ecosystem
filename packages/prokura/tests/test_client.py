# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

from dataclasses import dataclass, field
from typing import Any

import pytest

from prokura.client import ConnectionsClient, ConnectionsError
from prokura.contract import CONNECTION_GET_TOKEN, OAUTH_START


@dataclass
class _Error:
    code: str
    message: str


@dataclass
class _Response:
    ok: bool = True
    items: list[Any] = field(default_factory=list)
    object: dict[str, Any] = field(default_factory=dict)
    attrs: dict[str, Any] = field(default_factory=dict)
    error: _Error | None = None


class _Transport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, operation: str, payload: dict[str, Any]) -> _Response:
        self.calls.append((operation, payload))
        if operation == CONNECTION_GET_TOKEN:
            return _Response(
                object={"access_token": "server-side-result", "scope": ["mail:read"]},
                attrs={"has_token": True},
            )
        if operation == OAUTH_START:
            return _Response(object={"authorize_url": "https://example.test/authorize"})
        return _Response(ok=False, error=_Error("unsupported", operation))


@pytest.mark.asyncio
async def test_client_uses_operation_transport_and_returns_typed_token():
    transport = _Transport()
    token = await ConnectionsClient(transport).get_token("mail", "acc_1")
    assert token is not None
    assert token.access_token == "server-side-result"
    assert transport.calls == [
        (CONNECTION_GET_TOKEN, {"provider": "mail", "account_id": "acc_1"})
    ]


@pytest.mark.asyncio
async def test_client_preserves_structured_transport_error():
    with pytest.raises(ConnectionsError) as exc:
        await ConnectionsClient(_Transport()).status("mail")
    assert exc.value.code == "unsupported"
