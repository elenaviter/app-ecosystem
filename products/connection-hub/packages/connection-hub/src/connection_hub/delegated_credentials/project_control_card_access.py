# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Open and change a project's Control Card through the project (W260).

A project's Control Card is a credentialless Card stored under the person who
created it, and ``control_card_get``/``control_card_update`` find it only among
the caller's own Cards: every other admin of the project was answered
``control_card_not_found`` (applications#160 review, 2026-09-26). The project
host answers "may this person read or change this project's Control Card" under
the person's own session, and names the Card's creator; this service then reads
or writes under the creator's storage key, with the acting person recorded and
the edit bounded by what the acting person could delegate. The shape is W319's
agent Card path (``project_agent_card_access``); nothing is migrated and the
creator's own path is unchanged.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from dataclasses import replace as replace_fields
from typing import Any, Protocol

from connection_hub.delegated_credentials.named_service_policy import clean_text

CONTROL_CARD_READ = "read"
CONTROL_CARD_WRITE = "write"
# W260: attach (or detach) the project's Control Card to an agent's Card: the
# project host decides who may and names the Card's creator.
CONTROL_CARD_ATTACH = "attach"
CONTROL_CARD_ACTIONS = frozenset({CONTROL_CARD_READ, CONTROL_CARD_WRITE, CONTROL_CARD_ATTACH})
PROJECT_CONTROL_CARD_ATTACH_AUDIT_PROVENANCE = "project_control_card_attach_audit"
PROJECT_CONTROL_CARD_ATTACH_AUDIT_SCHEMA = "connection_hub.project_control_card_attach_audit.v1"
PROJECT_CONTROL_CARD_AUDIT_PROVENANCE = "project_control_card_audit"
PROJECT_CONTROL_CARD_AUDIT_SCHEMA = "connection_hub.project_control_card_audit.v1"
# Who the host may name: the project's owner, a person whose project Card holds
# project.control.update, or (to read only) a project member. Only the first
# two ever change the Card, whatever else a host answer says.
READING_VIAS = frozenset({"owner", "project_admin", "project_member"})
EDITING_VIAS = frozenset({"owner", "project_admin"})
NOT_DELEGABLE = "delegated_access_grants_not_delegable"


class ControlCardAuthorizationError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ProjectControlCardDecision:
    """The project host's answer for one Control Card, one action, one person."""

    allowed: bool
    via: str = ""
    reason: str = ""
    message: str = ""
    grantor_subject: str = ""
    control_id: str = ""
    project_ref: str = ""
    action: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Any) -> "ProjectControlCardDecision":
        if not isinstance(value, Mapping):
            raise ControlCardAuthorizationError("project_control_card_decision_invalid")
        allowed = value.get("allowed")
        if not isinstance(allowed, bool):
            raise ControlCardAuthorizationError("project_control_card_decision_invalid")
        evidence = value.get("evidence")
        if evidence is not None and not isinstance(evidence, Mapping):
            raise ControlCardAuthorizationError("project_control_card_decision_invalid")
        return cls(
            allowed=allowed,
            via=clean_text(value.get("via")),
            reason=clean_text(value.get("reason")),
            message=clean_text(value.get("message")),
            grantor_subject=clean_text(value.get("grantor_subject")),
            control_id=clean_text(value.get("control_id")),
            project_ref=clean_text(value.get("project_ref")),
            action=clean_text(value.get("action")),
            evidence=copy.deepcopy(dict(evidence or {})),
        )


class ProjectControlCardAuthorizationPort(Protocol):
    async def authorize_project_control_card(
        self, *, control_id: str, project_ref: str, action: str
    ) -> ProjectControlCardDecision: ...


class RefusingProjectControlCardAuthorizationPort:
    """Fail closed with a configuration reason.

    A missing provider is the deployment's gap, not a denial of this person:
    it is unavailable (503, with the reason), never a 403 that reads like
    "you may not" (review on app-ecosystem#187).
    """

    def __init__(self, reason: str) -> None:
        self._reason = reason

    async def authorize_project_control_card(
        self, *, control_id: str, project_ref: str, action: str
    ) -> ProjectControlCardDecision:
        raise ControlCardAuthorizationError(self._reason)


def _subject(user: Mapping[str, Any]) -> str:
    for key in ("user_id", "sub", "id"):
        value = clean_text(user.get(key))
        if value and value != "anonymous":
            return value
    return ""


def _invalid(reason: str) -> dict[str, Any]:
    return {"ok": False, "error": "project_control_card_authorization_invalid",
            "reason": reason, "retryable": True, "status": 503}


class ProjectControlCardAccess:
    """Read or change one project's Control Card for a person the project host authorizes."""

    def __init__(
        self,
        host: Any,
        port: ProjectControlCardAuthorizationPort | None,
        agent_access: Any = None,
    ) -> None:
        self._host = host
        self._port = port
        # W319's agent Card path, which answers for the agent side of an attach.
        self._agent_access = agent_access

    async def _authorize(
        self, user: Mapping[str, Any], *, control_id: str, project_ref: str, action: str
    ) -> ProjectControlCardDecision | dict[str, Any]:
        actor = _subject(user)
        if not actor or actor.startswith("integration:"):
            return {"ok": False, "error": "project_control_card_requires_person", "status": 401}
        control_id, project_ref = clean_text(control_id), clean_text(project_ref)
        if not control_id or not project_ref:
            return {"ok": False, "error": "project_control_card_request_invalid", "status": 400}
        if self._port is None:
            return {"ok": False, "error": "project_control_card_authorization_unavailable",
                    "reason": "authorization_port_not_configured", "retryable": True, "status": 503}
        try:
            decision = await self._port.authorize_project_control_card(
                control_id=control_id, project_ref=project_ref, action=action
            )
        except ControlCardAuthorizationError as exc:
            return {"ok": False, "error": "project_control_card_authorization_unavailable",
                    "reason": exc.reason, "retryable": True, "status": 503}
        except Exception:  # noqa: BLE001 - the project host is an availability boundary
            return {"ok": False, "error": "project_control_card_authorization_unavailable",
                    "reason": "authorization_port_failed", "retryable": True, "status": 503}
        if not isinstance(decision, ProjectControlCardDecision):
            return _invalid("decision_type_invalid")
        if not decision.allowed:
            refused = {"ok": False, "error": decision.reason or "project_control_card_denied", "status": 403}
            if decision.message:
                refused["message"] = decision.message
            return refused
        # The answer must be about this Card, this project and this action,
        # name the Card's creator, and a write must come from the owner or a
        # holder of project.control.update; anything else fails closed.
        if decision.control_id != control_id:
            return _invalid("decision_control_id_mismatch")
        if decision.project_ref != project_ref:
            return _invalid("decision_project_ref_mismatch")
        if decision.action != action:
            return _invalid("decision_action_mismatch")
        if not decision.grantor_subject:
            return _invalid("decision_grantor_missing")
        if decision.via not in READING_VIAS:
            return _invalid("decision_via_invalid")
        if action in {CONTROL_CARD_WRITE, CONTROL_CARD_ATTACH} and decision.via not in EDITING_VIAS:
            return {"ok": False, "error": "project_control_card_write_denied",
                    "reason": "decision_via_cannot_edit", "status": 403}
        return decision

    @staticmethod
    def _owner_user(decision: ProjectControlCardDecision) -> dict[str, Any]:
        """Storage identity only: the authorization came from the project host.

        It carries no roles or permissions, so the save runs without the
        acting person's platform roles: a Card offering a resource only a role
        may choose (an admin-only resource) cannot be saved on this path. The
        acting person's own grants still bound the save
        (``_actor_delegable_grants``).
        """

        return {"user_id": decision.grantor_subject, "roles": [], "permissions": []}

    @staticmethod
    def _not_found(result: Mapping[str, Any]) -> dict[str, Any]:
        # The creator the host named holds no credentialless Card by this id:
        # the host's link is stale or wrong, and nothing else is revealed.
        if result.get("error") == "control_card_not_found":
            return {"ok": False, "error": "project_control_card_not_found", "status": 404}
        return dict(result)

    async def get(self, user: Mapping[str, Any], *, control_id: str, project_ref: str) -> dict[str, Any]:
        decision = await self._authorize(user, control_id=control_id, project_ref=project_ref, action=CONTROL_CARD_READ)
        if isinstance(decision, dict):
            return decision
        result = await self._host.control_card_get(self._owner_user(decision), control_id=decision.control_id)
        if result.get("ok") is not True:
            return self._not_found(result)
        can_edit = decision.via in EDITING_VIAS
        if not can_edit:
            write = await self._authorize(user, control_id=control_id, project_ref=project_ref, action=CONTROL_CARD_WRITE)
            can_edit = isinstance(write, ProjectControlCardDecision)
        result = dict(result)
        result["access"] = {**dict(result.get("access") or {}), "via": decision.via,
                            "can_edit": bool(can_edit), "project_ref": decision.project_ref}
        return result

    async def _actor_delegable_grants(self, user: Mapping[str, Any], grantor_subject: str) -> list[str]:
        """What the acting person could delegate: an editor never grants beyond it."""

        offer = await self._host._offer_config(owner_subject=grantor_subject)
        if offer is None:
            return []
        return sorted((await self._host._available_inventory(user, config=offer)).grant_names())

    def _audit(self, user: Mapping[str, Any], decision: ProjectControlCardDecision, *, request_id: str):
        occurred_at = int(time.time())
        actor = _subject(user)

        def stamp(previous: Any, candidate: Any) -> Any:
            provenance = dict(getattr(candidate, "provenance", None) or {})
            provenance[PROJECT_CONTROL_CARD_AUDIT_PROVENANCE] = {
                "schema": PROJECT_CONTROL_CARD_AUDIT_SCHEMA,
                "action": "updated",
                "actor_subject": actor,
                "via": decision.via,
                "project_ref": decision.project_ref,
                "request_id": clean_text(request_id),
                "occurred_at": occurred_at,
                "before_revision": int(getattr(previous, "card_revision", 0) or 0),
                "after_revision": int(getattr(candidate, "card_revision", 0) or 0),
                "evidence": copy.deepcopy(dict(decision.evidence or {})),
            }
            return replace_fields(candidate, provenance=provenance)

        return stamp

    async def update(
        self,
        user: Mapping[str, Any],
        *,
        control_id: str,
        project_ref: str,
        request_id: str = "",
        **changes: Any,
    ) -> dict[str, Any]:
        decision = await self._authorize(user, control_id=control_id, project_ref=project_ref, action=CONTROL_CARD_WRITE)
        if isinstance(decision, dict):
            return decision
        result = await self._host.control_card_update(
            self._owner_user(decision),
            control_id=decision.control_id,
            _delegable_grants=await self._actor_delegable_grants(user, decision.grantor_subject),
            _record_transform=self._audit(user, decision, request_id=request_id),
            **changes,
        )
        if result.get("ok") is not True:
            if result.get("error") == NOT_DELEGABLE:
                return self._not_delegable(result)
            return self._not_found(result)
        result = dict(result)
        result["access"] = {**dict(result.get("access") or {}), "via": decision.via,
                            "can_edit": True, "project_ref": decision.project_ref}
        return result

    @staticmethod
    def _not_delegable(result: Mapping[str, Any]) -> dict[str, Any]:
        """Say why a save the editor did not widen is still refused (review on app-ecosystem#187).

        A save is checked against every permission the Card carries after it,
        not only the ones the editor added, so an editor who holds fewer
        permissions than the Card carries can save no change at all.
        """

        refused = dict(result)
        grants = ", ".join(str(grant) for grant in refused.get("grants") or ()) or "some of its permissions"
        refused["message"] = (
            "To save this project's Control Card you must be able to delegate every permission it "
            f"carries, not only the ones you change. You cannot delegate: {grants}. The Card's creator, "
            "or an admin who holds these permissions, can save it; or remove them from the Card first."
        )
        return refused

    # -- attaching the project's Control Card to an agent (W260) ----------------

    async def _attach_decisions(
        self, user: Mapping[str, Any], *, control_id: str, project_ref: str, access_id: str
    ) -> tuple[ProjectControlCardDecision, Any] | dict[str, Any]:
        """Both host answers an attach or detach needs, or the first refusal.

        The project host decides the Control Card side (``attach``) and names
        its creator; W319's agent question decides the agent side and names its
        owner, and here alone accepts a project admin linking an agent that
        does not attend the project yet. Detaching widens the agent, so it asks
        the same two questions.
        """

        control = await self._authorize(
            user, control_id=control_id, project_ref=project_ref, action=CONTROL_CARD_ATTACH
        )
        if isinstance(control, dict):
            return control
        if self._agent_access is None:
            return {"ok": False, "error": "project_control_card_authorization_unavailable",
                    "reason": "agent_card_authorization_not_configured", "retryable": True, "status": 503}
        agent = await self._agent_access._authorize(
            user, access_id=access_id, project_ref=project_ref, action="write", allow_linking=True
        )
        if isinstance(agent, dict):
            return agent
        return control, agent

    def _attach_audit(
        self,
        user: Mapping[str, Any],
        control: ProjectControlCardDecision,
        agent: Any,
        *,
        action: str,
        request_id: str,
    ):
        occurred_at = int(time.time())
        actor = _subject(user)

        def stamp(previous: Any, candidate: Any) -> Any:
            provenance = dict(getattr(candidate, "provenance", None) or {})
            provenance[PROJECT_CONTROL_CARD_ATTACH_AUDIT_PROVENANCE] = {
                "schema": PROJECT_CONTROL_CARD_ATTACH_AUDIT_SCHEMA,
                "action": action,
                "actor_subject": actor,
                "project_ref": control.project_ref,
                "control_id": control.control_id,
                "control_holder": control.grantor_subject,
                "control_via": control.via,
                "agent_via": str(getattr(agent, "via", "") or ""),
                "request_id": clean_text(request_id),
                "occurred_at": occurred_at,
                "before_revision": int(getattr(previous, "card_revision", 0) or 0),
                "after_revision": int(getattr(candidate, "card_revision", 0) or 0),
            }
            return replace_fields(candidate, provenance=provenance)

        return stamp

    @staticmethod
    def _agent_owner(agent: Any) -> dict[str, Any]:
        """Storage identity only: the agent's Card is changed under its owner's key."""

        return {"user_id": str(agent.grantor_subject), "roles": [], "permissions": []}

    async def attach(
        self,
        user: Mapping[str, Any],
        *,
        control_id: str,
        project_ref: str,
        access_id: str,
        replace_control_id: str = "",
        expected_card_revision: int | None = None,
        request_id: str = "",
    ) -> dict[str, Any]:
        decided = await self._attach_decisions(
            user, control_id=control_id, project_ref=project_ref, access_id=clean_text(access_id)
        )
        if isinstance(decided, dict):
            return decided
        control, agent = decided
        return await self._host.attach_control_card(
            self._agent_owner(agent),
            access_id=clean_text(access_id),
            control_id=control.control_id,
            expected_card_revision=expected_card_revision,
            replace_control_id=clean_text(replace_control_id),
            _control_holder=control.grantor_subject,
            _record_transform=self._attach_audit(
                user, control, agent, action="attached", request_id=request_id
            ),
        )

    async def detach(
        self,
        user: Mapping[str, Any],
        *,
        control_id: str,
        project_ref: str,
        access_id: str,
        expected_card_revision: int | None = None,
        request_id: str = "",
    ) -> dict[str, Any]:
        decided = await self._attach_decisions(
            user, control_id=control_id, project_ref=project_ref, access_id=clean_text(access_id)
        )
        if isinstance(decided, dict):
            return decided
        control, agent = decided
        return await self._host.detach_control_card(
            self._agent_owner(agent),
            access_id=clean_text(access_id),
            control_id=control.control_id,
            expected_card_revision=expected_card_revision,
            _record_transform=self._attach_audit(
                user, control, agent, action="detached", request_id=request_id
            ),
            _through_project=True,
        )
