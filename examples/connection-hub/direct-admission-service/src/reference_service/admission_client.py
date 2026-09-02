from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any, Mapping

import httpx
from connection_hub.delegated_credentials.admission import (
    SERVICE_ID_HEADER,
    SERVICE_NONCE_HEADER,
    SERVICE_SIGNATURE_HEADER,
    SERVICE_TIMESTAMP_HEADER,
    AdmissionRequest,
    sign_admission_request,
)

from reference_service.settings import Settings


@dataclass(frozen=True)
class AdmissionResult:
    status_code: int
    payload: Mapping[str, Any]

    @property
    def allowed(self) -> bool:
        return self.status_code == 200 and self.payload.get("allowed") is True


class AdmissionClient:
    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._client = client

    async def evaluate(
        self,
        *,
        delegated_bearer: str,
        operation: str,
        invocation_id: str,
        request_digest: str,
    ) -> AdmissionResult:
        request = AdmissionRequest(
            resource=self._settings.resource,
            operation=operation,
            invocation_id=invocation_id,
            request_digest=request_digest,
        )
        timestamp = str(int(time.time()))
        nonce = secrets.token_urlsafe(24)
        signature = sign_admission_request(
            secret=self._settings.service_secret,
            service_id=self._settings.service_id,
            timestamp=timestamp,
            nonce=nonce,
            delegated_token=delegated_bearer,
            request=request,
        )
        headers = {
            "Authorization": f"Bearer {delegated_bearer}",
            SERVICE_ID_HEADER: self._settings.service_id,
            SERVICE_TIMESTAMP_HEADER: timestamp,
            SERVICE_NONCE_HEADER: nonce,
            SERVICE_SIGNATURE_HEADER: signature,
        }
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=10.0)
        try:
            response = await client.post(
                self._settings.admission_url,
                headers=headers,
                json=request.signing_dict(),
            )
        finally:
            if owns_client:
                await client.aclose()

        try:
            payload = response.json()
        except ValueError:
            payload = {
                "ok": False,
                "allowed": False,
                "error": {
                    "code": "admission_response_invalid",
                    "message": "The admission service returned a non-JSON response.",
                    "retryable": response.status_code >= 500,
                },
            }
        if not isinstance(payload, Mapping):
            payload = {
                "ok": False,
                "allowed": False,
                "error": {
                    "code": "admission_response_invalid",
                    "message": "The admission service returned an invalid response.",
                    "retryable": False,
                },
            }
        return AdmissionResult(status_code=response.status_code, payload=payload)


__all__ = ["AdmissionClient", "AdmissionResult"]
