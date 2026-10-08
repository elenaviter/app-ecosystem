# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter
"""Forward one managed Card edit to the project host that coordinates it.

While Card transactions are enabled, the Hub's direct writers refuse a managed
Card (a project Control, a person Control bound under it, a project-bound
agent Card). The edit is made instead by a transaction the project host
coordinates (card-transactions.md, activation condition 9). This module is the
Hub's half of that hand-over, and it stays generic: it knows Cards, not roles.

- The request names the person the Hub authenticated (``actor_subject``, from
  the platform session only), the scope, a stable ``request_id``, the exact
  target and its revision, and the complete selection the person saw.
- It is signed with the Hub's admission proof under the caller descriptor's
  authority request signer, with its own protocol (``managed-card-edit.v1``),
  so it never verifies as a plan authorization or an authority answer.
- The host decides authority, plans, prepares, commits and finishes. It
  answers ``{ok: true, outcome: {...}}`` with ``state`` ``committed``,
  ``aborted`` or ``pending``. ``pending`` (or a lost answer) is retried with
  the same ``request_id``; the host replays its one decision.
"""

from __future__ import annotations

import os
import time
from typing import Any, Awaitable, Callable, Mapping

from service_foundation.coordination.durable_wire import canonical_json_bytes, sha256_hex

from .admission import AdmissionRequest, sign_admission_request

PROTOCOL = "managed-card-edit.v1"
OPERATION = "project_card_edit"
SCHEMA = "managed-card-edit.v1"
OUTCOME_SCHEMA = "managed-card-edit-outcome.v1"
TARGET_KINDS = frozenset({"person_control", "agent_card", "invitation_control"})
SELECTION_FIELDS = ("resource_grants", "resource_operations", "named_service_operations", "account_scope")

BundleCall = Callable[..., Awaitable[Any]]


class ManagedCardEditError(ValueError):
    def __init__(self, reason: str, status: int = 409, message: str = "") -> None:
        super().__init__(reason)
        self.reason, self.status, self.message = reason, status, message

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": self.reason, "status": self.status,
                "message": self.message or "The project did not save this Card change."}


def managed_card_edit_body(*, actor_subject: str, project_ref: str, request_id: str, kind: str,
                           principal_key: str, original_revision: int, selection: Mapping[str, Any],
                           access_id: str | None = None, subject_hash: str | None = None) -> dict:
    """The exact unsigned request; nothing in it comes from the browser except the edit itself."""
    for value in (actor_subject, project_ref, request_id, principal_key):
        if type(value) is not str or not 0 < len(value) <= 256 or value != value.strip() or not value.isprintable():
            raise ManagedCardEditError("managed_card_edit_request_invalid", 400)
    if kind in {"agent_card", "invitation_control"}:
        # The host cannot read an agent Card: it names the Card's own storage
        # coordinates, and the Hub's planner keeps every field not sent.
        if (principal_key != "card:" + str(access_id) or type(subject_hash) is not str or not subject_hash
                or type(original_revision) is not int or original_revision < 1 or not isinstance(selection, Mapping)
                or not selection or set(selection) - set(SELECTION_FIELDS)
                or any(not isinstance(value, Mapping) for value in selection.values())):
            raise ManagedCardEditError("managed_card_edit_request_invalid", 400)
        return {"schema": SCHEMA, "actor_subject": actor_subject, "project_ref": project_ref, "request_id": request_id,
                "target": {"kind": kind, "access_id": access_id, "subject_hash": subject_hash,
                           "original_revision": original_revision},
                "selection": {field: dict(selection[field]) for field in SELECTION_FIELDS if field in selection}}
    # Only the fields the person changed travel; the host keeps every other
    # field of the revision-fenced original, never an empty default.
    if (kind not in TARGET_KINDS or type(original_revision) is not int or original_revision < 1
            or not isinstance(selection, Mapping) or not selection or set(selection) - set(SELECTION_FIELDS)
            or any(not isinstance(value, Mapping) for value in selection.values())):
        raise ManagedCardEditError("managed_card_edit_request_invalid", 400)
    return {"schema": SCHEMA, "actor_subject": actor_subject, "project_ref": project_ref, "request_id": request_id,
            "target": {"kind": kind, "principal_key": principal_key, "original_revision": original_revision},
            "selection": {field: dict(selection[field]) for field in SELECTION_FIELDS if field in selection}}


def sign_managed_card_edit(body: Mapping[str, Any], *, bundle_id: str, signer_id: str, secret: str,
                           clock: Callable[[], float] = time.time) -> dict[str, str]:
    """The Hub's ``service_proof`` over exactly ``body``."""
    proof = {"service_id": signer_id, "timestamp": str(int(clock())), "nonce": os.urandom(16).hex()}
    proof["signature"] = sign_admission_request(
        secret=secret, **proof, delegated_token=f"{PROTOCOL}:{body['request_id']}",
        request=AdmissionRequest(resource=bundle_id, operation=OPERATION, invocation_id=body["request_id"],
                                 request_digest=sha256_hex(canonical_json_bytes(dict(body))),
                                 approval_context={"protocol": PROTOCOL}))
    return proof


def _outcome(answer: Any, body: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(answer, Mapping):
        raise ManagedCardEditError("managed_card_edit_answer_invalid", 502)
    if answer.get("ok") is not True:
        error = answer.get("error")
        code = error.get("code") if isinstance(error, Mapping) else error
        message = error.get("message") if isinstance(error, Mapping) else answer.get("message")
        status = answer.get("status")
        raise ManagedCardEditError(str(code or "managed_card_edit_refused"),
                                   status if type(status) is int and 400 <= status < 600 else 409,
                                   str(message or ""))
    outcome = answer.get("outcome")
    if (not isinstance(outcome, Mapping)
            or set(outcome) != {"schema", "request_id", "state", "transaction_id", "card_revision"}
            or outcome["schema"] != OUTCOME_SCHEMA or outcome["request_id"] != body["request_id"]
            or outcome["state"] not in {"committed", "aborted", "pending"}
            or type(outcome["transaction_id"]) is not str or not outcome["transaction_id"]
            or type(outcome["card_revision"]) is not int):
        raise ManagedCardEditError("managed_card_edit_answer_invalid", 502)
    return dict(outcome)


class PeerManagedCardEdit:
    """The project host's ``project_card_edit``, for one configured Card transaction caller."""

    def __init__(self, *, call: BundleCall, bundle_id: str, signer_id: str, secret: str,
                 clock: Callable[[], float] = time.time) -> None:
        self._call, self._bundle_id, self._signer_id, self._secret, self._clock = (
            call, bundle_id, signer_id, secret, clock)

    async def forward(self, body: Mapping[str, Any]) -> dict[str, Any]:
        proof = sign_managed_card_edit(body, bundle_id=self._bundle_id, signer_id=self._signer_id,
                                       secret=self._secret, clock=self._clock)
        try:
            answer = await self._call(bundle_id=self._bundle_id, operation=OPERATION,
                                      data={**body, "service_proof": proof})
        except Exception:  # noqa: BLE001 - transport failure: the outcome is unknown, retry the same request
            raise ManagedCardEditError("managed_card_edit_outcome_unknown", 503,
                                       "The project did not answer. Retry the same change.") from None
        return _outcome(answer, body)


__all__ = ["OPERATION", "OUTCOME_SCHEMA", "PROTOCOL", "SCHEMA", "ManagedCardEditError", "PeerManagedCardEdit",
           "managed_card_edit_body", "sign_managed_card_edit"]
