"""Validate an A2A 1.0 Agent Card and reduce it to the fields a caller pins.

Everything in a card is the remote agent's text and is untrusted. Only the
fields a caller needs survive, each bounded: the JSON-RPC interface for
protocol 1.x, the skills (with ids a caller can name safely), and the names
and kinds of the declared security schemes. The pinned digest covers exactly
those fields, so any change a caller would act on is drift.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping

from app_foundation.a2a.errors import A2A_CARD_INVALID, A2A_VERSION_UNSUPPORTED, A2AClientError

JSONRPC_BINDING = "JSONRPC"
MAX_SKILLS = 32
MAX_NAME_CHARS = 200
MAX_DESCRIPTION_CHARS = 1000
MAX_TAGS = 16
MAX_TAG_CHARS = 64
MAX_SECURITY_SCHEMES = 16
SKILL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SECURITY_SCHEME_KINDS = frozenset(
    {
        "apiKeySecurityScheme",
        "httpAuthSecurityScheme",
        "oauth2SecurityScheme",
        "openIdConnectSecurityScheme",
        "mtlsSecurityScheme",
    }
)


def bounded_text(value: Any, limit: int) -> str:
    """One line of printable text, at most ``limit`` characters."""

    text = value if isinstance(value, str) else ""
    cleaned = "".join(
        " " if unicodedata.category(char)[0] in {"C", "Z"} else char for char in text
    )
    return " ".join(cleaned.split())[:limit]


@dataclass(frozen=True, slots=True)
class AgentSkill:
    skill_id: str
    name: str
    description: str
    tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PinnedAgentCard:
    card_url: str
    name: str
    description: str
    version: str
    jsonrpc_url: str
    protocol_version: str
    skills: tuple[AgentSkill, ...]
    security_schemes: tuple[tuple[str, str], ...]
    dropped_skills: int
    digest: str

    def pinned_fields(self) -> dict[str, Any]:
        return _pinned_fields(
            name=self.name,
            description=self.description,
            version=self.version,
            jsonrpc_url=self.jsonrpc_url,
            protocol_version=self.protocol_version,
            skills=self.skills,
            security_schemes=self.security_schemes,
        )

    def skill(self, skill_id: str) -> AgentSkill | None:
        return next((skill for skill in self.skills if skill.skill_id == skill_id), None)


def _pinned_fields(
    *,
    name: str,
    description: str,
    version: str,
    jsonrpc_url: str,
    protocol_version: str,
    skills: tuple[AgentSkill, ...],
    security_schemes: tuple[tuple[str, str], ...],
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "version": version,
        "jsonrpc_url": jsonrpc_url,
        "protocol_version": protocol_version,
        "skills": [
            {
                "id": skill.skill_id,
                "name": skill.name,
                "description": skill.description,
                "tags": list(skill.tags),
            }
            for skill in skills
        ],
        "security_schemes": [list(item) for item in security_schemes],
    }


def card_digest(fields: Mapping[str, Any]) -> str:
    encoded = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _jsonrpc_interface(raw: Mapping[str, Any]) -> tuple[str, str]:
    interfaces = raw.get("supportedInterfaces")
    if not isinstance(interfaces, list):
        # A 0.3 card names its endpoint at the top level; 1.0 lists interfaces.
        raise A2AClientError(A2A_VERSION_UNSUPPORTED)
    for entry in interfaces:
        if not isinstance(entry, Mapping):
            continue
        binding = entry.get("protocolBinding")
        version = entry.get("protocolVersion")
        url = entry.get("url")
        if binding != JSONRPC_BINDING or not isinstance(version, str) or not isinstance(url, str):
            continue
        if version.split(".", 1)[0] == "1" and url.strip():
            return url.strip(), version.strip()[:16]
    raise A2AClientError(A2A_VERSION_UNSUPPORTED)


def _skills(raw: Mapping[str, Any]) -> tuple[tuple[AgentSkill, ...], int]:
    entries = raw.get("skills")
    if not isinstance(entries, list):
        raise A2AClientError(A2A_CARD_INVALID)
    skills: list[AgentSkill] = []
    seen: set[str] = set()
    dropped = 0
    for entry in entries:
        skill_id = entry.get("id") if isinstance(entry, Mapping) else None
        if (
            not isinstance(skill_id, str)
            or not SKILL_ID_PATTERN.fullmatch(skill_id)
            or skill_id in seen
            or len(skills) >= MAX_SKILLS
        ):
            dropped += 1
            continue
        seen.add(skill_id)
        tags = entry.get("tags") if isinstance(entry.get("tags"), list) else []
        skills.append(
            AgentSkill(
                skill_id=skill_id,
                name=bounded_text(entry.get("name"), MAX_NAME_CHARS) or skill_id,
                description=bounded_text(entry.get("description"), MAX_DESCRIPTION_CHARS),
                tags=tuple(
                    tag
                    for tag in (bounded_text(item, MAX_TAG_CHARS) for item in tags[:MAX_TAGS])
                    if tag
                ),
            )
        )
    return tuple(sorted(skills, key=lambda skill: skill.skill_id)), dropped


def _security_schemes(raw: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    schemes = raw.get("securitySchemes")
    if schemes is None:
        return ()
    if not isinstance(schemes, Mapping):
        raise A2AClientError(A2A_CARD_INVALID)
    out: list[tuple[str, str]] = []
    for name, scheme in list(schemes.items())[:MAX_SECURITY_SCHEMES]:
        label = bounded_text(name, MAX_TAG_CHARS)
        kinds = [key for key in scheme if key in SECURITY_SCHEME_KINDS] if isinstance(scheme, Mapping) else []
        if label and len(kinds) == 1:
            out.append((label, kinds[0]))
    return tuple(sorted(out))


def parse_agent_card(raw: Any, *, card_url: str) -> PinnedAgentCard:
    """Validate a decoded card. Raises ``A2AClientError`` with a fixed code."""

    if not isinstance(raw, Mapping):
        raise A2AClientError(A2A_CARD_INVALID)
    jsonrpc_url, protocol_version = _jsonrpc_interface(raw)
    name = bounded_text(raw.get("name"), MAX_NAME_CHARS)
    if not name:
        raise A2AClientError(A2A_CARD_INVALID)
    skills, dropped = _skills(raw)
    fields = dict(
        name=name,
        description=bounded_text(raw.get("description"), MAX_DESCRIPTION_CHARS),
        version=bounded_text(raw.get("version"), MAX_TAG_CHARS),
        jsonrpc_url=jsonrpc_url,
        protocol_version=protocol_version,
        skills=skills,
        security_schemes=_security_schemes(raw),
    )
    return PinnedAgentCard(
        card_url=card_url,
        dropped_skills=dropped,
        digest=card_digest(_pinned_fields(**fields)),
        **fields,
    )


__all__ = [
    "AgentSkill",
    "JSONRPC_BINDING",
    "PinnedAgentCard",
    "SKILL_ID_PATTERN",
    "bounded_text",
    "card_digest",
    "parse_agent_card",
]
