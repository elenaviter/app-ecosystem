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

import logging
import os
import re
import time
from typing import Any, Awaitable, Callable, Mapping

from service_foundation.coordination.durable_wire import canonical_json_bytes, sha256_hex

from ..bundle_operations import BundleOperationResultError, normalize_bundle_operation_result
from .admission import AdmissionRequest, sign_admission_request

PROTOCOL = "managed-card-edit.v1"
OPERATION = "project_card_edit"
SCHEMA = "managed-card-edit.v1"
OUTCOME_SCHEMA = "managed-card-edit-outcome.v1"
TARGET_KINDS = frozenset({"person_control", "person_my", "agent_card", "invitation_control", "project_control"})
SELECTION_FIELDS = ("resource_grants", "resource_operations", "named_service_operations", "account_scope")

BundleCall = Callable[..., Awaitable[Any]]
_LOG = logging.getLogger(__name__)
_CODE = re.compile(r"[a-z][a-z0-9_.]{0,79}")


class ManagedCardEditError(ValueError):
    def __init__(self, reason: str, status: int = 409, message: str = "") -> None:
        super().__init__(reason)
        self.reason, self.status, self.message = reason, status, message

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": self.reason, "status": self.status,
                "message": self.message or "The project did not save this Card change."}


def _accepted_part(value: Any) -> dict[str, list[str]]:
    """W723: the person's "Accept update" decisions, ``{resource: [operation, ...]}``, canonical; {} for none."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ManagedCardEditError("managed_card_edit_request_invalid", 400)
    out: dict[str, list[str]] = {}
    for resource, names in value.items():
        if (type(resource) is not str or not resource or isinstance(names, (str, bytes))
                or not hasattr(names, "__iter__")):
            raise ManagedCardEditError("managed_card_edit_request_invalid", 400)
        listed = list(names)
        if not listed or any(type(name) is not str or not name for name in listed):
            raise ManagedCardEditError("managed_card_edit_request_invalid", 400)
        out[resource] = sorted(set(listed))
    return dict(sorted(out.items()))


def managed_card_edit_body(*, actor_subject: str, project_ref: str, request_id: str, kind: str,
                           principal_key: str, original_revision: int, selection: Mapping[str, Any],
                           access_id: str | None = None, subject_hash: str | None = None,
                           accepted_operations: Mapping[str, Any] | None = None,
                           expected_catalog_version: str | None = None) -> dict:
    """The exact unsigned request; nothing in it comes from the browser except the edit itself.

    W723: ``accepted_operations`` travels only when the person ticked "Accept update", so every body
    without it is byte-identical to before. An acceptance alone is a real edit: ``selection`` may then
    be empty. W724: ``person_my`` is the person's own My Card (``principal_key`` is their own ``user:``).
    """
    for value in (actor_subject, project_ref, request_id, principal_key):
        if type(value) is not str or not 0 < len(value) <= 256 or value != value.strip() or not value.isprintable():
            raise ManagedCardEditError("managed_card_edit_request_invalid", 400)
    accepted = _accepted_part(accepted_operations)
    if not isinstance(selection, Mapping) or (not selection and not accepted):
        raise ManagedCardEditError("managed_card_edit_request_invalid", 400)
    if kind == "person_my" and principal_key != "user:" + actor_subject:
        raise ManagedCardEditError("managed_card_edit_request_invalid", 400)  # only one's own My Card
    extra = {"accepted_operations": accepted} if accepted else {}
    if accepted and expected_catalog_version is not None:
        # The catalog the person reviewed: an acceptance is never applied to a catalog they did not see.
        if (type(expected_catalog_version) is not str or not 0 < len(expected_catalog_version) <= 256
                or expected_catalog_version != expected_catalog_version.strip()):
            raise ManagedCardEditError("managed_card_edit_request_invalid", 400)
        extra["expected_catalog_version"] = expected_catalog_version
    if kind in {"agent_card", "invitation_control", "project_control"}:
        # The host cannot read an agent Card or the project Control (P): it names
        # the Card's own storage coordinates, and the Hub's planner keeps every
        # field not sent.
        if (principal_key != "card:" + str(access_id) or type(subject_hash) is not str or not subject_hash
                or type(original_revision) is not int or original_revision < 1 or not isinstance(selection, Mapping)
                or set(selection) - set(SELECTION_FIELDS)
                or any(not isinstance(value, Mapping) for value in selection.values())):
            raise ManagedCardEditError("managed_card_edit_request_invalid", 400)
        return {"schema": SCHEMA, "actor_subject": actor_subject, "project_ref": project_ref, "request_id": request_id,
                "target": {"kind": kind, "access_id": access_id, "subject_hash": subject_hash,
                           "original_revision": original_revision},
                "selection": {field: dict(selection[field]) for field in SELECTION_FIELDS if field in selection},
                **extra}
    # Only the fields the person changed travel; the host keeps every other
    # field of the revision-fenced original, never an empty default.
    if (kind not in TARGET_KINDS or type(original_revision) is not int or original_revision < 1
            or set(selection) - set(SELECTION_FIELDS)
            or any(not isinstance(value, Mapping) for value in selection.values())):
        raise ManagedCardEditError("managed_card_edit_request_invalid", 400)
    return {"schema": SCHEMA, "actor_subject": actor_subject, "project_ref": project_ref, "request_id": request_id,
            "target": {"kind": kind, "principal_key": principal_key, "original_revision": original_revision},
            "selection": {field: dict(selection[field]) for field in SELECTION_FIELDS if field in selection},
            **extra}


def managed_card_location(kind: str, *, project_ref: str, ref: str) -> tuple[str, str]:
    """Where a managed person Control or pending invitation Control is stored: (access_id, grantor subject).

    The project-domain identities stay here, outside the generic Card core.
    """
    try:
        if kind == "person_control":
            from .controls.project_person import ProjectPersonControlIdentity
            identity = ProjectPersonControlIdentity.build(project_ref=project_ref, target_subject=ref)
            return identity.control_id, identity.project_subject
        if kind == "invitation_control":
            from .controls.project_invitation import project_invitation_control_id
            from .controls.project_person import project_authority_subject
            return project_invitation_control_id(project_ref, ref), project_authority_subject(project_ref)
    except ValueError as exc:  # both identity errors are ValueErrors; an invalid identity names no Card
        raise ManagedCardEditError("managed_card_edit_request_invalid", 400) from exc
    raise ManagedCardEditError("managed_card_edit_request_invalid", 400)


def managed_my_card_owner(authority: Any) -> tuple[str, str] | None:
    """W724: ``(project_ref, person_subject)`` when ``authority`` is a person's project My Card, else None.

    The project-domain identity stays here, outside the generic Card core.
    """
    from .project_identity_lifecycle import ProjectIdentityLifecycleError, ProjectPersonCardIdentity
    try:
        identity = ProjectPersonCardIdentity.from_my_card(authority)
    except (ProjectIdentityLifecycleError, ValueError):
        return None
    return identity.project_ref, identity.person_subject


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
        # Live 2026-10-09 22:13Z: a refused Control save showed only "The project did not save this Card change."
        # and neither side logged why. One value-free line: the target kind and the project's fixed code. A code
        # is logged only from the structured ``error.code`` field (the fixed-code contract); a flat error string
        # is free text whatever its spelling, so it is never logged (CodeApp return on #720).
        structured = error.get("code") if isinstance(error, Mapping) else None
        _LOG.warning("managed card edit refused by the project: kind=%s code=%s status=%s",
                     (body.get("target") or {}).get("kind", "-"),
                     structured if isinstance(structured, str) and _CODE.fullmatch(structured) else "-",
                     status if type(status) is int else "-")
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
        from ..card_save_trace import hop, trace_scope
        started = time.monotonic()
        with trace_scope(body.get("request_id")):
            hop("hub.card_edit_forward", "entry")
            try:
                outcome = await self._forward(body)
            except ManagedCardEditError as exc:
                hop("hub.card_edit_forward", "refused", code=exc.reason, started=started)
                raise
            except Exception:
                hop("hub.card_edit_forward", "error", started=started)
                raise
            hop("hub.card_edit_forward", "ok", started=started)
            return outcome

    async def _forward(self, body: Mapping[str, Any]) -> dict[str, Any]:
        proof = sign_managed_card_edit(body, bundle_id=self._bundle_id, signer_id=self._signer_id,
                                       secret=self._secret, clock=self._clock)
        try:
            # Live 2026-10-09 23:01Z: the platform adds the signed-in session's user_id/fingerprint to an
            # operation's arguments unless they are given; the project's exact signed body then carried an extra
            # field and was refused as invalid. As the project's own Hub calls do, the body travels whole under
            # "data" and the identity hints are given as None: the person is the signed actor_subject.
            answer = await self._call(bundle_id=self._bundle_id, operation=OPERATION,
                                      data={"data": {**body, "service_proof": proof},
                                            "user_id": None, "fingerprint": None})
        except Exception:  # noqa: BLE001 - transport failure: the outcome is unknown, retry the same request
            raise ManagedCardEditError("managed_card_edit_outcome_unknown", 503,
                                       "The project did not answer. Retry the same change.") from None
        try:
            # The operation route answers {"status": "ok", ..., "<operation>": answer}; read the project's own answer.
            answer = normalize_bundle_operation_result(OPERATION, answer)
        except BundleOperationResultError:
            raise ManagedCardEditError("managed_card_edit_answer_invalid", 502) from None
        return _outcome(answer, body)


__all__ = ["OPERATION", "OUTCOME_SCHEMA", "PROTOCOL", "SCHEMA", "ManagedCardEditError", "PeerManagedCardEdit",
           "managed_card_edit_body", "managed_card_location", "sign_managed_card_edit"]


# W729: no project route ends one managed Card on its own (card-transactions.md, "Editing a managed Card from the
# Hub": a managed edit carries a selection, never a state change). Each kind ends through the project action that
# owns it (Problem Board cards.md and flows.md), so the Hub's W578 refusal names that action for the screen.
MANAGED_REVOKE_REASONS = {
    "person_my": ("managed_card_revoke_requires_project_removal",
                  "A person's My Card ends only when the person is removed from the project, which ends their "
                  "project Control Card and My Card together. Use People > Remove in Problem Board. "
                  "Nothing was changed."),
    "agent": ("managed_card_revoke_requires_unlink",
              "This agent's Card is linked to a project. Unlink the agent from the project in Problem Board "
              "first; its own Card can then be revoked here. Nothing was changed."),
    "invitation_control": ("managed_card_revoke_requires_invitation_withdraw",
                           "This is a pending invitation's Control Card. Withdraw the invitation in Problem Board "
                           "(People > Invitations). Nothing was changed."),
    "project_control": ("managed_card_revoke_project_control_unsupported",
                        "A project's Control Card cannot be revoked from here; no project route ends it yet. "
                        "Nothing was changed."),
    "managed": ("managed_card_revoke_requires_project",
                "This Card is managed by its project. It ends only through the project's own action in "
                "Problem Board. Nothing was changed."),
}


def managed_revoke_kind(record: Any, *, authority_from_record: Callable[[Any], Any], agent_source: str) -> str:
    """Which managed kind a refused revoke ends: the project Control, a pending invitation's Control, a person's
    My Card, an agent Card, or other."""
    from .controls.project_invitation import PROJECT_INVITATION_CONTROL_ISSUER_KIND

    if str(getattr(record, "issuer_kind", "") or "").strip() == PROJECT_INVITATION_CONTROL_ISSUER_KIND:
        return "invitation_control"
    try:
        bound = record.control_card is not None
    except AttributeError:
        bound = False
    if str(getattr(record, "issuer_kind", "") or "").strip() == "application" and not bound:
        return "project_control"
    try:
        owner = managed_my_card_owner(authority_from_record(record))
    except (AttributeError, TypeError, ValueError):  # not a complete Card record: not a My Card
        owner = None
    if owner is not None:
        return "person_my"
    if str(getattr(record, "source", "") or "").strip() == agent_source:
        return "agent"
    return "managed"


def managed_revoke_reason(record: Any, *, authority_from_record: Callable[[Any], Any],
                          agent_source: str) -> tuple[str, str]:
    """``(reason, message)`` for a refused revoke of a managed Card (W729)."""
    return MANAGED_REVOKE_REASONS[managed_revoke_kind(record, authority_from_record=authority_from_record,
                                                      agent_source=agent_source)]
