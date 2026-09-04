# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Stable identity of a resident caller profile and its delegated card.

A resident caller is a hosted agent: one agent of one application acting for
one grantor. Its delegated card must keep one ``access_id`` while the resources
on it change, because everything that binds to the card (the reusable bearer,
invocation policies, consent recovery links, audit) is keyed by that id. The
earlier deterministic id folded the selected resource set into the hash, so
adding a resource addressed a different record and the profile fragmented into
one card per resource.

The stable key is:

    grantor subject
    tenant/project scope    (implied by the store the card lives in)
    resident application    (the app/bundle the agent is defined in)
    agent id

Selected resources and their acting scopes are card CONTENTS, never part of
the key. A resident caller has one card. The current Card mutation contract
accepts resources with one compatible acting scope and refuses a conflicting
scope; it never creates another card for the same resident caller.

This module is the only place the formula lives. Projection and Gateway read
``ResidentCallerProfile`` instead of reproducing hashing.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Iterable

RESIDENT_CLIENT_PREFIX = "kdcube-agent:"
RESIDENT_ACCESS_ID_PREFIX = "agent-"
# Versioned so a later change of the key produces distinct ids rather than a
# silent collision with records written under this formula.
RESIDENT_PROFILE_KEY_VERSION = "resident-profile-v1"
_ID_HASH_CHARS = 16


def _clean(value: Any) -> str:
    return str(value or "").strip()


def is_resident_client_id(client_id: Any) -> bool:
    """Whether a delegated client id names a hosted agent."""
    return _clean(client_id).startswith(RESIDENT_CLIENT_PREFIX)


def resident_client_id(application: str, agent_id: str) -> str:
    """``kdcube-agent:<app>:<agent>``, the delegated client identity of one
    hosted agent. Mirrors ``connection_hub.delegated_mcp.delegated_client_id_for_agent``."""
    app = _clean(application)
    agent = _clean(agent_id)
    if not app or not agent:
        raise ValueError("resident client id needs an application and an agent id")
    return f"{RESIDENT_CLIENT_PREFIX}{app}:{agent}"


def stable_resident_access_id(
    grantor_subject: str,
    client_id: str,
) -> str:
    """The one card id of a resident profile, independent of card contents."""
    grantor = _clean(grantor_subject)
    client = _clean(client_id)
    if not grantor or not client:
        raise ValueError("stable resident access id needs a grantor and a client id")
    key = f"{grantor}|{client}|{RESIDENT_PROFILE_KEY_VERSION}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:_ID_HASH_CHARS]
    return f"{RESIDENT_ACCESS_ID_PREFIX}{digest}"


def legacy_resident_access_id(
    grantor_subject: str, client_id: str, resources: Iterable[str]
) -> str:
    """The pre-stable id: one record per (grantor, client, selected resource
    set). Kept so migration can find the records written under it; nothing new
    is written under this formula."""
    selected = sorted({_clean(r) for r in (resources or ()) if _clean(r)})
    digest = hashlib.sha256(
        f"{_clean(grantor_subject)}|{_clean(client_id)}|{'+'.join(selected)}".encode("utf-8")
    ).hexdigest()[:_ID_HASH_CHARS]
    return f"{RESIDENT_ACCESS_ID_PREFIX}{digest}"


@dataclass(frozen=True)
class ResidentCallerProfile:
    """One hosted agent acting for one grantor."""

    grantor_subject: str
    application: str
    agent_id: str

    def __post_init__(self) -> None:
        grantor = _clean(self.grantor_subject)
        app = _clean(self.application)
        agent = _clean(self.agent_id)
        if not grantor or not app or not agent:
            raise ValueError("resident profile needs grantor, application, and agent id")
        object.__setattr__(self, "grantor_subject", grantor)
        object.__setattr__(self, "application", app)
        object.__setattr__(self, "agent_id", agent)

    @classmethod
    def parse(
        cls,
        grantor_subject: str,
        client_id: str,
    ) -> "ResidentCallerProfile | None":
        """The profile behind a ``kdcube-agent:<app>:<agent>`` client id, or
        ``None`` for every other client family (manual automations, OAuth
        clients), which keep their own independent identities."""
        client = _clean(client_id)
        grantor = _clean(grantor_subject)
        if not grantor or not client.startswith(RESIDENT_CLIENT_PREFIX):
            return None
        rest = client[len(RESIDENT_CLIENT_PREFIX):]
        app, sep, agent = rest.partition(":")
        if not sep or not _clean(app) or not _clean(agent):
            return None
        return cls(
            grantor_subject=grantor,
            application=app,
            agent_id=agent,
        )

    @property
    def client_id(self) -> str:
        return resident_client_id(self.application, self.agent_id)

    @property
    def access_id(self) -> str:
        return stable_resident_access_id(self.grantor_subject, self.client_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "resident",
            "grantor_subject": self.grantor_subject,
            "application": self.application,
            "agent_id": self.agent_id,
            "client_id": self.client_id,
            "access_id": self.access_id,
        }


__all__ = [
    "RESIDENT_ACCESS_ID_PREFIX",
    "RESIDENT_CLIENT_PREFIX",
    "RESIDENT_PROFILE_KEY_VERSION",
    "ResidentCallerProfile",
    "is_resident_client_id",
    "legacy_resident_access_id",
    "resident_client_id",
    "stable_resident_access_id",
]
