from __future__ import annotations

import gzip
import time
from types import SimpleNamespace

import pytest

import connection_hub_cli.management.secret_export as secret_export_module
from connection_hub_cli.authorization.pkce import code_challenge
from connection_hub_cli.errors import AuthorizationError
from connection_hub_cli.management import ManagementSecretTarget, ManagementTarget
from connection_hub_cli.management.secret_export import (
    BrowserSecretExportService,
    ExportedSecret,
    HttpxSecretExportTransport,
    SECRET_EXPORT_RESULT_SCHEMA,
    SECRET_EXPORT_START_SCHEMA,
    SecretExportClient,
    SecretExportRequest,
    SecretExportResult,
    SecretExportStart,
)


class _Transport:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.responses: list[tuple[int, dict]] = []

    async def post(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def _target() -> ManagementTarget:
    return ManagementTarget.create(
        public_base_url="https://runtime.example",
        tenant="tenant-a",
        project="project-a",
    )


def _request() -> SecretExportRequest:
    verifier = "v" * 64
    return SecretExportRequest.create(
        target=_target(),
        callback_uri="http://127.0.0.1:51234/callback",
        state="s" * 43,
        code_challenge=code_challenge(verifier),
        targets=[
            ManagementSecretTarget.create(
                scope="platform",
                key="services.brave.api_key",
            ),
            ManagementSecretTarget.create(
                scope="bundle",
                bundle_id="connection-hub@1-0",
                key="connections.oauth_state_secret",
            ),
        ],
    )


def _result_payload(
    request: SecretExportRequest,
    start: SecretExportStart,
    *,
    verified_at: int,
) -> dict:
    return {
        "schema": SECRET_EXPORT_RESULT_SCHEMA,
        "ok": True,
        "transaction_id": start.transaction_id,
        "request_digest": request.request_digest,
        "target": {"tenant": "tenant-a", "project": "project-a"},
        "approval": {
            "assurance": "session_confirmation",
            "method": "kdcube_platform_browser_session",
            "verified_at": verified_at,
        },
        "values": [
            {
                "scope": "bundle",
                "bundle_id": "connection-hub@1-0",
                "key": "connections.oauth_state_secret",
                "value": "bundle-secret-marker",
            },
            {
                "scope": "platform",
                "key": "services.brave.api_key",
                "value": "platform-secret-marker",
            },
        ],
    }


@pytest.mark.asyncio
async def test_client_validates_exact_start_and_export_result() -> None:
    transport = _Transport()
    client = SecretExportClient(transport=transport)
    request = _request()
    transaction_id = "t" * 43
    transport.responses = [
        (
            200,
            {
                "schema": SECRET_EXPORT_START_SCHEMA,
                "ok": True,
                "transaction_id": transaction_id,
                "request_digest": request.request_digest,
                "authorization_url": (
                    "https://runtime.example/api/integrations/management/v1/"
                    f"secrets/export/authorize?transaction={transaction_id}"
                ),
                "required_assurance": "session_confirmation",
                "expires_at": int(time.time()) + 180,
            },
        ),
        (
            200,
            {
                "schema": SECRET_EXPORT_RESULT_SCHEMA,
                "ok": True,
                "transaction_id": transaction_id,
                "request_digest": request.request_digest,
                "target": {"tenant": "tenant-a", "project": "project-a"},
                "approval": {
                    "assurance": "session_confirmation",
                    "method": "kdcube_platform_browser_session",
                    "verified_at": int(time.time()),
                },
                "values": [
                    {
                        "scope": "bundle",
                        "bundle_id": "connection-hub@1-0",
                        "key": "connections.oauth_state_secret",
                        "value": "bundle-secret-marker",
                    },
                    {
                        "scope": "platform",
                        "key": "services.brave.api_key",
                        "value": "platform-secret-marker",
                    },
                ],
            },
        ),
    ]

    started = await client.start(request)
    result = await client.exchange(
        request,
        started,
        code="c" * 43,
        code_verifier="v" * 64,
    )

    assert started.request_digest == request.request_digest
    assert result.approval_verified_at <= int(time.time())
    assert [item.target.identity for item in result.values] == [
        ("bundle", "connection-hub@1-0", "connections.oauth_state_secret"),
        ("platform", "", "services.brave.api_key"),
    ]
    assert "bundle-secret-marker" not in repr(result)
    assert "platform-secret-marker" not in repr(result)
    assert transport.calls[0]["payload"] == request.payload
    assert transport.calls[1]["payload"] == {
        "transaction_id": transaction_id,
        "code": "c" * 43,
        "code_verifier": "v" * 64,
    }


@pytest.mark.asyncio
async def test_client_rejects_authorization_url_on_another_origin() -> None:
    transport = _Transport()
    client = SecretExportClient(transport=transport)
    request = _request()
    transaction_id = "t" * 43
    transport.responses = [
        (
            200,
            {
                "schema": SECRET_EXPORT_START_SCHEMA,
                "ok": True,
                "transaction_id": transaction_id,
                "request_digest": request.request_digest,
                "authorization_url": (
                    "https://attacker.example/api/integrations/management/v1/"
                    f"secrets/export/authorize?transaction={transaction_id}"
                ),
                "required_assurance": "session_confirmation",
                "expires_at": int(time.time()) + 180,
            },
        )
    ]

    with pytest.raises(AuthorizationError) as raised:
        await client.start(request)

    assert raised.value.code == "secret_export_response_invalid"


@pytest.mark.asyncio
async def test_client_rejects_unbounded_transaction_expiry() -> None:
    transport = _Transport()
    client = SecretExportClient(transport=transport)
    request = _request()
    transaction_id = "t" * 43
    transport.responses = [
        (
            200,
            {
                "schema": SECRET_EXPORT_START_SCHEMA,
                "ok": True,
                "transaction_id": transaction_id,
                "request_digest": request.request_digest,
                "authorization_url": (
                    "https://runtime.example/api/integrations/management/v1/"
                    f"secrets/export/authorize?transaction={transaction_id}"
                ),
                "required_assurance": "session_confirmation",
                "expires_at": int(time.time()) + 901,
            },
        )
    ]

    with pytest.raises(AuthorizationError) as raised:
        await client.start(request)

    assert raised.value.code == "secret_export_response_invalid"


@pytest.mark.asyncio
async def test_client_rejects_value_for_an_unrequested_target() -> None:
    transport = _Transport()
    client = SecretExportClient(transport=transport)
    request = _request()
    transaction_id = "t" * 43
    transport.responses = [
        (
            200,
            {
                "schema": SECRET_EXPORT_START_SCHEMA,
                "ok": True,
                "transaction_id": transaction_id,
                "request_digest": request.request_digest,
                "authorization_url": (
                    "https://runtime.example/api/integrations/management/v1/"
                    f"secrets/export/authorize?transaction={transaction_id}"
                ),
                "required_assurance": "session_confirmation",
                "expires_at": int(time.time()) + 180,
            },
        ),
        (
            200,
            {
                "schema": SECRET_EXPORT_RESULT_SCHEMA,
                "ok": True,
                "transaction_id": transaction_id,
                "request_digest": request.request_digest,
                "target": {"tenant": "tenant-a", "project": "project-a"},
                "approval": {
                    "assurance": "session_confirmation",
                    "method": "kdcube_platform_browser_session",
                    "verified_at": int(time.time()),
                },
                "values": [
                    {
                        "scope": "bundle",
                        "bundle_id": "connection-hub@1-0",
                        "key": "connections.oauth_state_secret",
                        "value": "bundle-secret-marker",
                    },
                    {
                        "scope": "platform",
                        "key": "services.openai.api_key",
                        "value": "wrong-secret-marker",
                    },
                ],
            },
        ),
    ]

    started = await client.start(request)
    with pytest.raises(AuthorizationError) as raised:
        await client.exchange(
            request,
            started,
            code="c" * 43,
            code_verifier="v" * 64,
        )

    assert raised.value.code == "secret_export_response_invalid"


@pytest.mark.asyncio
async def test_client_rejects_values_above_protocol_total(monkeypatch) -> None:
    transport = _Transport()
    client = SecretExportClient(transport=transport)
    request = _request()
    start = SecretExportStart(
        transaction_id="t" * 43,
        request_digest=request.request_digest,
        authorization_url="https://runtime.example/export/authorize",
        required_assurance="session_confirmation",
        expires_at=int(time.time()) + 180,
    )
    monkeypatch.setattr(secret_export_module, "MAX_EXPORTED_SECRET_TOTAL_BYTES", 5)
    transport.responses = [
        (
            200,
            {
                "schema": SECRET_EXPORT_RESULT_SCHEMA,
                "ok": True,
                "transaction_id": start.transaction_id,
                "request_digest": request.request_digest,
                "target": {"tenant": "tenant-a", "project": "project-a"},
                "approval": {
                    "assurance": "session_confirmation",
                    "method": "kdcube_platform_browser_session",
                    "verified_at": int(time.time()),
                },
                "values": [
                    {
                        **request.targets[0].to_dict(),
                        "value": "abc",
                    },
                    {
                        **request.targets[1].to_dict(),
                        "value": "def",
                    },
                ],
            },
        )
    ]

    with pytest.raises(AuthorizationError) as raised:
        await client.exchange(
            request,
            start,
            code="c" * 43,
            code_verifier="v" * 64,
        )

    assert raised.value.code == "secret_export_response_invalid"
    assert "abc" not in str(raised.value)
    assert "def" not in str(raised.value)


@pytest.mark.asyncio
async def test_client_rejects_assurance_downgrade_at_exchange() -> None:
    transport = _Transport()
    client = SecretExportClient(transport=transport)
    request = _request()
    start = SecretExportStart(
        transaction_id="t" * 43,
        request_digest=request.request_digest,
        authorization_url="https://runtime.example/export/authorize",
        required_assurance="fresh_authentication",
        expires_at=int(time.time()) + 180,
    )
    transport.responses = [
        (
            200,
            {
                "schema": SECRET_EXPORT_RESULT_SCHEMA,
                "ok": True,
                "transaction_id": start.transaction_id,
                "request_digest": request.request_digest,
                "target": {"tenant": "tenant-a", "project": "project-a"},
                "approval": {
                    "assurance": "session_confirmation",
                    "method": "kdcube_platform_browser_session",
                    "verified_at": int(time.time()),
                },
                "values": [
                    {
                        "scope": "bundle",
                        "bundle_id": "connection-hub@1-0",
                        "key": "connections.oauth_state_secret",
                        "value": "bundle-secret-marker",
                    },
                    {
                        "scope": "platform",
                        "key": "services.brave.api_key",
                        "value": "platform-secret-marker",
                    },
                ],
            },
        )
    ]

    with pytest.raises(AuthorizationError) as raised:
        await client.exchange(
            request,
            start,
            code="c" * 43,
            code_verifier="v" * 64,
        )

    assert raised.value.code == "secret_export_response_invalid"


@pytest.mark.asyncio
async def test_client_rejects_stale_approval_evidence() -> None:
    transport = _Transport()
    client = SecretExportClient(transport=transport)
    request = _request()
    start = SecretExportStart(
        transaction_id="t" * 43,
        request_digest=request.request_digest,
        authorization_url="https://runtime.example/export/authorize",
        required_assurance="session_confirmation",
        expires_at=int(time.time()) + 180,
    )
    transport.responses = [
        (
            200,
            _result_payload(
                request,
                start,
                verified_at=int(time.time()) - 901,
            ),
        )
    ]

    with pytest.raises(AuthorizationError) as raised:
        await client.exchange(
            request,
            start,
            code="c" * 43,
            code_verifier="v" * 64,
        )

    assert raised.value.code == "secret_export_response_invalid"


@pytest.mark.asyncio
async def test_http_transport_rejects_duplicate_response_fields() -> None:
    import httpx2

    async def duplicate(_request):
        return httpx2.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=b'{"ok":true,"ok":false}',
        )

    transport = HttpxSecretExportTransport(
        transport=httpx2.MockTransport(duplicate),
    )

    with pytest.raises(AuthorizationError) as raised:
        await transport.post(url="https://runtime.example/export", payload={})

    assert raised.value.code == "secret_export_response_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "content"),
    [
        (
            [
                ("Content-Type", "application/json"),
                ("Content-Type", "application/json"),
            ],
            b'{"ok":true}',
        ),
        (
            [
                ("Content-Type", "application/json"),
                ("Content-Encoding", "gzip"),
            ],
            gzip.compress(b'{"ok":true}'),
        ),
    ],
)
async def test_http_transport_rejects_ambiguous_or_encoded_content(
    headers: list[tuple[str, str]],
    content: bytes,
) -> None:
    import httpx2

    async def response(_request):
        return httpx2.Response(200, headers=headers, content=content)

    transport = HttpxSecretExportTransport(
        transport=httpx2.MockTransport(response),
    )

    with pytest.raises(AuthorizationError) as raised:
        await transport.post(url="https://runtime.example/export", payload={})

    assert raised.value.code == "secret_export_response_invalid"


@pytest.mark.asyncio
async def test_browser_service_closes_callback_after_exact_exchange() -> None:
    callbacks = []
    opened = []

    class _Callback:
        redirect_uri = "http://127.0.0.1:51234/callback"

        def __init__(self, **kwargs) -> None:
            self.options = kwargs
            self.closed = False
            callbacks.append(self)

        def wait(self, *, timeout_seconds):
            assert timeout_seconds == 17
            return SimpleNamespace(code="c" * 43)

        def close(self) -> None:
            self.closed = True

    class _Client:
        def __init__(self) -> None:
            self.request = None
            self.exchange_args = None

        async def start(self, request):
            self.request = request
            return SecretExportStart(
                transaction_id="t" * 43,
                request_digest=request.request_digest,
                authorization_url="https://runtime.example/export/authorize",
                required_assurance="session_confirmation",
                expires_at=int(time.time()) + 180,
            )

        async def exchange(self, request, start, **kwargs):
            self.exchange_args = (request, start, kwargs)
            return SecretExportResult(
                transaction_id=start.transaction_id,
                request_digest=request.request_digest,
                assurance="session_confirmation",
                approval_method="browser_session",
                approval_verified_at=int(time.time()),
                values=tuple(
                    ExportedSecret(target=item, value="secret-marker")
                    for item in request.targets
                ),
            )

    client = _Client()
    service = BrowserSecretExportService(
        client=client,
        browser_opener=lambda url: opened.append(url) is None,
        callback_factory=_Callback,
    )
    targets = [
        ManagementSecretTarget.create(
            scope="platform",
            key="services.brave.api_key",
        )
    ]

    result = await service.export(
        target=_target(),
        targets=targets,
        timeout_seconds=17,
    )

    assert len(result.values) == 1
    assert opened == ["https://runtime.example/export/authorize"]
    assert callbacks[0].options["expected_issuer"] == "https://runtime.example"
    assert callbacks[0].options["issuer_required"] is True
    assert callbacks[0].closed is True
    assert client.exchange_args[2]["code"] == "c" * 43
