# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Open and change an agent's Card through the project it attends (W319).

A Card is stored and changed under its owner. Two more people must reach an
agent's Card (operator, 2026-09-25/26): every admin of a project the agent
attends now opens and changes it, and a platform admin opens (reads) every
Card. The project host answers "may this person do this to this Card" under the
person's own session, like project membership; this service then reads or
writes under the owner's storage key, with the acting person recorded. No
credential is copied and no Card is re-approved.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from dataclasses import replace as replace_fields
from connection_hub.delegated_credentials.named_service_policy import clean_text

AGENT_CARD_READ = "read"
AGENT_CARD_WRITE = "write"
PROJECT_AGENT_CARD_AUDIT_PROVENANCE = "project_agent_card_audit"
PROJECT_AGENT_CARD_AUDIT_SCHEMA = "connection_hub.project_agent_card_audit.v1"
EDITING_VIAS = frozenset({"owner", "project_admin"})


class AgentCardAuthorizationError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason



@dataclass(frozen=True)
class AgentCardDecision:
    """The project host's answer for one Card, one action, one person."""

    allowed: bool
    via: str = ""
    reason: str = ""
    message: str = ""
    grantor_subject: str = ""
    worker_name: str = ""
    access_id: str = ""
    project_ref: str = ""
    action: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Any) -> "AgentCardDecision":
        if not isinstance(value, Mapping):
            raise AgentCardAuthorizationError("project_agent_card_decision_invalid")
        decision = cls(
            allowed=value.get("allowed") is True,
            via=clean_text(value.get("via")),
            reason=clean_text(value.get("reason")),
            message=clean_text(value.get("message")),
            grantor_subject=clean_text(value.get("grantor_subject")),
            worker_name=clean_text(value.get("worker_name")),
            access_id=clean_text(value.get("access_id")),
            project_ref=clean_text(value.get("project_ref")),
            action=clean_text(value.get("action")),
            evidence=dict(value.get("evidence") or {}) if isinstance(value.get("evidence"), Mapping) else {},
        )
        if decision.allowed and not decision.grantor_subject:
            raise AgentCardAuthorizationError("project_agent_card_decision_grantor_missing")
        return decision


class AgentCardAuthorizationPort(Protocol):
    async def authorize_agent_card(
        self, *, access_id: str, project_ref: str, action: str
    ) -> AgentCardDecision: ...


class RefusingAgentCardAuthorizationPort:
    """Fail closed with a configuration reason."""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    async def authorize_agent_card(self, *, access_id: str, project_ref: str, action: str) -> AgentCardDecision:
        return AgentCardDecision(allowed=False, reason=self._reason, access_id=access_id,
                                 project_ref=project_ref, action=action)


def _subject(user: Mapping[str, Any]) -> str:
    for key in ("user_id", "sub", "id"):
        value = clean_text(user.get(key))
        if value and value != "anonymous":
            return value
    return ""


class ProjectAgentCardAccess:
    """Read or change one agent Card for a person the project host authorizes."""

    def __init__(self, host: Any, port: AgentCardAuthorizationPort | None) -> None:
        self._host = host
        self._port = port

    async def _authorize(
        self, user: Mapping[str, Any], *, access_id: str, project_ref: str, action: str
    ) -> AgentCardDecision | dict[str, Any]:
        actor = _subject(user)
        if not actor or actor.startswith("integration:"):
            return {"ok": False, "error": "project_agent_card_requires_person", "status": 401}
        access_id = clean_text(access_id)
        if not access_id:
            return {"ok": False, "error": "project_agent_card_requires_access_id", "status": 400}
        if self._port is None:
            return {"ok": False, "error": "project_agent_card_authorization_unavailable",
                    "reason": "authorization_port_not_configured", "retryable": True, "status": 503}
        try:
            decision = await self._port.authorize_agent_card(
                access_id=access_id, project_ref=clean_text(project_ref), action=action
            )
        except AgentCardAuthorizationError as exc:
            return {"ok": False, "error": "project_agent_card_authorization_unavailable",
                    "reason": exc.reason, "retryable": True, "status": 503}
        except Exception:  # noqa: BLE001 - the project host is an availability boundary
            return {"ok": False, "error": "project_agent_card_authorization_unavailable",
                    "reason": "authorization_port_failed", "retryable": True, "status": 503}
        if not isinstance(decision, AgentCardDecision):
            return {"ok": False, "error": "project_agent_card_authorization_invalid", "retryable": True, "status": 503}
        if not decision.allowed:
            refused = {"ok": False, "error": decision.reason or "project_agent_card_denied", "status": 403}
            if decision.message:
                refused["message"] = decision.message
            return refused
        # The answer must be about this Card and this action, and a write must
        # come from the owner or a project admin; anything else fails closed
        # (review on app-ecosystem#161).
        if decision.access_id != access_id:
            return {"ok": False, "error": "project_agent_card_authorization_invalid",
                    "reason": "decision_access_id_mismatch", "retryable": True, "status": 503}
        if decision.action != action:
            return {"ok": False, "error": "project_agent_card_authorization_invalid",
                    "reason": "decision_action_mismatch", "retryable": True, "status": 503}
        if action == AGENT_CARD_WRITE and decision.via not in EDITING_VIAS:
            return {"ok": False, "error": "project_agent_card_write_denied",
                    "reason": "decision_via_cannot_edit", "status": 403}
        return decision

    @staticmethod
    def _owner_user(decision: AgentCardDecision) -> dict[str, Any]:
        """Storage identity only: the authorization came from the project host."""

        return {"user_id": decision.grantor_subject, "roles": [], "permissions": []}

    async def get(self, user: Mapping[str, Any], *, access_id: str, project_ref: str) -> dict[str, Any]:
        decision = await self._authorize(user, access_id=access_id, project_ref=project_ref, action=AGENT_CARD_READ)
        if isinstance(decision, dict):
            return decision
        listing = await self._host.list_access(self._owner_user(decision))
        if listing.get("ok") is not True:
            return listing
        item = next(
            (dict(row) for row in listing.get("items") or [] if clean_text(row.get("access_id")) == clean_text(access_id)),
            None,
        )
        if item is None:
            return {"ok": False, "error": "project_agent_card_not_found", "status": 404}
        can_edit = decision.via in EDITING_VIAS
        if not can_edit:
            write = await self._authorize(user, access_id=access_id, project_ref=project_ref, action=AGENT_CARD_WRITE)
            can_edit = isinstance(write, AgentCardDecision)
        return {
            "ok": True,
            "item": item,
            "grant_options": await self._host.grant_options(user),
            "resources": await self._host.resource_options(user),
            "access": {
                "via": decision.via,
                "can_edit": bool(can_edit),
                "project_ref": clean_text(project_ref),
                "worker_name": decision.worker_name,
            },
        }

    async def _actor_delegable_grants(self, user: Mapping[str, Any], grantor_subject: str) -> list[str]:
        """What the acting person could delegate: a project admin never grants beyond it."""

        offer = await self._host._offer_config(owner_subject=grantor_subject)
        if offer is None:
            return []
        return sorted((await self._host._available_inventory(user, config=offer)).grant_names())

    def _audit(self, user: Mapping[str, Any], decision: AgentCardDecision, *, action: str, request_id: str):
        occurred_at = int(time.time())
        actor = _subject(user)

        def stamp(previous: Any, candidate: Any) -> Any:
            provenance = dict(getattr(candidate, "provenance", None) or {})
            provenance[PROJECT_AGENT_CARD_AUDIT_PROVENANCE] = {
                "schema": PROJECT_AGENT_CARD_AUDIT_SCHEMA,
                "action": action,
                "actor_subject": actor,
                "via": decision.via,
                "project_ref": decision.project_ref,
                "request_id": clean_text(request_id),
                "occurred_at": occurred_at,
                "before_revision": int(getattr(previous, "card_revision", 0) or 0),
                "after_revision": int(getattr(candidate, "card_revision", 0) or 0),
            }
            return replace_fields(candidate, provenance=provenance)

        return stamp

    async def update(
        self,
        user: Mapping[str, Any],
        *,
        access_id: str,
        project_ref: str,
        request_id: str = "",
        **changes: Any,
    ) -> dict[str, Any]:
        decision = await self._authorize(user, access_id=access_id, project_ref=project_ref, action=AGENT_CARD_WRITE)
        if isinstance(decision, dict):
            return decision
        delegable = await self._actor_delegable_grants(user, decision.grantor_subject)
        return await self._host.update_access(
            self._owner_user(decision),
            access_id=clean_text(access_id),
            _delegable_grants=delegable,
            _platform_admin=False,
            _record_transform=self._audit(user, decision, action="updated", request_id=request_id),
            **changes,
        )

    async def apply_profile(
        self,
        user: Mapping[str, Any],
        *,
        access_id: str,
        project_ref: str,
        profile: str,
        expected_card_revision: int | None = None,
        request_id: str = "",
    ) -> dict[str, Any]:
        decision = await self._authorize(user, access_id=access_id, project_ref=project_ref, action=AGENT_CARD_WRITE)
        if isinstance(decision, dict):
            return decision
        return await self._host.apply_authorization_profile(
            self._owner_user(decision),
            access_id=clean_text(access_id),
            profile=profile,
            expected_card_revision=expected_card_revision,
            request_id=request_id,
            _actor_subject=_subject(user),
            _delegable_grants=await self._actor_delegable_grants(user, decision.grantor_subject),
            _extra_record_transform=self._audit(user, decision, action="profile_applied", request_id=request_id),
        )
