# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Typed client for Connection Hub request authenticators."""

from __future__ import annotations

from typing import Any, Mapping

from connection_hub.authenticators.models import AuthenticatedRequest, RequestEnvelope
from connection_hub.connection_edges import BundleOperationCaller


DEFAULT_CONNECTION_HUB_BUNDLE_ID = "connection-hub@1-0"
REQUEST_AUTHENTICATE_OPERATION = "request_authenticate"


def _str(value: Any) -> str:
    return str(value or "").strip()


class ConnectionHubAuthenticatorsClient:
    """Client for request-authentication operations served by Connection Hub."""

    def __init__(
        self,
        *,
        connection_hub_bundle_id: str = DEFAULT_CONNECTION_HUB_BUNDLE_ID,
        tenant: str | None = None,
        project: str | None = None,
        operation_caller: BundleOperationCaller | None = None,
    ) -> None:
        self.bundle_id = _str(connection_hub_bundle_id) or DEFAULT_CONNECTION_HUB_BUNDLE_ID
        self.tenant = _str(tenant) or None
        self.project = _str(project) or None
        self._operation_caller = operation_caller

    async def authenticate_request(
        self,
        request: Any,
        *,
        include_body: bool = False,
    ) -> AuthenticatedRequest:
        envelope = (
            request
            if isinstance(request, RequestEnvelope)
            else await RequestEnvelope.from_request(request, include_body=include_body)
        )
        return await self.authenticate_envelope(envelope)

    async def authenticate_envelope(
        self,
        request: RequestEnvelope | Mapping[str, Any],
    ) -> AuthenticatedRequest:
        envelope = RequestEnvelope.coerce(request)
        if self._operation_caller is None:
            raise RuntimeError(
                "ConnectionHubAuthenticatorsClient requires an application-operation caller"
            )
        result = await self._operation_caller(
            bundle_id=self.bundle_id,
            operation=REQUEST_AUTHENTICATE_OPERATION,
            data={"request": envelope.to_dict()},
            tenant=self.tenant,
            project=self.project,
            route="public",
        )
        return AuthenticatedRequest.coerce(result)


__all__ = [
    "ConnectionHubAuthenticatorsClient",
    "DEFAULT_CONNECTION_HUB_BUNDLE_ID",
    "REQUEST_AUTHENTICATE_OPERATION",
]
