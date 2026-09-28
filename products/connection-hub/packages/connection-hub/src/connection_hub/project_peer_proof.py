# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Connection Hub's proof on its question to the project host (W371).

Before Connection Hub gives an agent its owner's GitHub token, it asks the
project host (Problem Board) whether that agent attends the project, may use
GitHub there, and names a repository on the project card. The call reaches a
public board operation, and a platform peer call does not say which app made
it, so Connection Hub signs the question.

The scheme is the admission ServiceProof (delegated_credentials/admission.py)
in the other direction, with a secret the operator provisions for the two
apps alone (never an admission-service registration):

- signer ``service_id`` ``connection-hub``;
- ``delegated_token`` ``connection-hub:<access_id>``, which, with the
  operation name, keeps a proof in this direction from passing as an
  admission proof and back;
- ``AdmissionRequest(resource=<board bundle id>, operation=
  "project_agent_github_authorize", approval_context={access_id,
  grantor_subject, project_ref, repository})``;
- the proof travels in the body as ``service_proof``.

The board verifies with :func:`verify_github_authorize_request` and claims
each nonce once within ``DEFAULT_NONCE_TTL_SECONDS``.
"""

from __future__ import annotations

import secrets
import time
from typing import Any, Mapping

from connection_hub.delegated_credentials.admission import (
    DEFAULT_MAX_CLOCK_SKEW_SECONDS,
    AdmissionRequest,
    ServiceProof,
    ServiceProofDecision,
    sign_admission_request,
    verify_admission_request,
)

PEER_PROOF_SERVICE_ID = "connection-hub"
GITHUB_AUTHORIZE_OPERATION = "project_agent_github_authorize"
_FIELDS = ("access_id", "grantor_subject", "project_ref", "repository")


def _token(access_id: str) -> str:
    return f"{PEER_PROOF_SERVICE_ID}:{access_id}"


def _request(board_bundle_id: str, fields: Mapping[str, Any]) -> AdmissionRequest:
    return AdmissionRequest(
        resource=str(board_bundle_id or "").strip(),
        operation=GITHUB_AUTHORIZE_OPERATION,
        approval_context={key: str(fields.get(key) or "").strip() for key in _FIELDS},
    )


def sign_github_authorize_request(
    *,
    secret: str | bytes,
    board_bundle_id: str,
    access_id: str,
    grantor_subject: str,
    project_ref: str,
    repository: str,
    now: int | None = None,
    nonce: str | None = None,
) -> dict[str, Any]:
    """The body Connection Hub sends: the four fields plus ``service_proof``."""

    fields = {
        "access_id": access_id,
        "grantor_subject": grantor_subject,
        "project_ref": project_ref,
        "repository": repository,
    }
    timestamp = str(int(time.time()) if now is None else int(now))
    nonce = nonce or secrets.token_urlsafe(24)
    signature = sign_admission_request(
        secret=secret,
        service_id=PEER_PROOF_SERVICE_ID,
        timestamp=timestamp,
        nonce=nonce,
        delegated_token=_token(access_id),
        request=_request(board_bundle_id, fields),
    )
    return {
        **fields,
        "service_proof": {
            "service_id": PEER_PROOF_SERVICE_ID,
            "timestamp": timestamp,
            "nonce": nonce,
            "signature": signature,
        },
    }


def verify_github_authorize_request(
    *,
    secret: str | bytes,
    board_bundle_id: str,
    body: Mapping[str, Any],
    max_clock_skew_seconds: int = DEFAULT_MAX_CLOCK_SKEW_SECONDS,
    now: int | None = None,
) -> ServiceProofDecision:
    """The board's check of a body; nonce replay is the caller's to claim."""

    raw = body.get("service_proof") if isinstance(body, Mapping) else None
    if not isinstance(raw, Mapping):
        return ServiceProofDecision(False, "service_proof_missing")
    proof = ServiceProof(
        service_id=str(raw.get("service_id") or ""),
        timestamp=str(raw.get("timestamp") or ""),
        nonce=str(raw.get("nonce") or ""),
        signature=str(raw.get("signature") or ""),
    )
    if proof.service_id != PEER_PROOF_SERVICE_ID:
        return ServiceProofDecision(False, "service_id_invalid")
    access_id = str(body.get("access_id") or "").strip()
    if not access_id:
        return ServiceProofDecision(False, "access_id_missing")
    return verify_admission_request(
        secret=secret,
        proof=proof,
        delegated_token=_token(access_id),
        request=_request(board_bundle_id, body),
        max_clock_skew_seconds=max_clock_skew_seconds,
        now=now,
    )


__all__ = [
    "GITHUB_AUTHORIZE_OPERATION",
    "PEER_PROOF_SERVICE_ID",
    "sign_github_authorize_request",
    "verify_github_authorize_request",
]
