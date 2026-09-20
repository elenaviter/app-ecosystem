# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Typed KDCube application resources for delegated capability boundaries."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, unquote

APPLICATION_RESOURCE_PREFIX = "urn:kdcube:app:"
APPLICATION_RESOURCE_SELECTOR_TYPE = "kdcube_application"

_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@-]{0,511}$")


class ApplicationResourceError(ValueError):
    """An application resource or selector is not canonical."""


def _component(value: str, *, name: str, allow_star: bool = False) -> str:
    text = str(value or "").strip()
    if allow_star and text == "*":
        return text
    if (
        not text
        or not _COMPONENT_RE.fullmatch(text)
        or ".." in text
        or any(marker in text for marker in ("*", "?", "[", "]"))
    ):
        raise ApplicationResourceError(f"{name}_invalid")
    return text


@dataclass(frozen=True)
class ApplicationResource:
    """One application/agent target inside an exact deployment.

    ``*`` is valid only as a complete application or agent segment. It never
    consumes ``:`` and therefore cannot escape the tenant/project boundary.
    """

    tenant: str
    project: str
    application: str
    agent: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "tenant", _component(self.tenant, name="tenant"))
        object.__setattr__(self, "project", _component(self.project, name="project"))
        object.__setattr__(
            self,
            "application",
            _component(self.application, name="application", allow_star=True),
        )
        object.__setattr__(
            self,
            "agent",
            _component(self.agent, name="agent", allow_star=True),
        )

    @classmethod
    def parse(cls, value: str) -> "ApplicationResource":
        text = str(value or "").strip()
        if not text.startswith(APPLICATION_RESOURCE_PREFIX):
            raise ApplicationResourceError("application_resource_invalid")
        segments = text[len(APPLICATION_RESOURCE_PREFIX) :].split(":")
        if len(segments) != 4:
            raise ApplicationResourceError("application_resource_invalid")
        try:
            decoded = [unquote(segment, errors="strict") for segment in segments]
        except UnicodeError as exc:
            raise ApplicationResourceError("application_resource_invalid") from exc
        resource = cls(*decoded)
        if resource.resource != text:
            raise ApplicationResourceError("application_resource_not_canonical")
        return resource

    @property
    def broad(self) -> bool:
        return self.application == "*" or self.agent == "*"

    @property
    def exact(self) -> bool:
        return not self.broad

    @property
    def resource(self) -> str:
        encoded = (
            quote(self.tenant, safe="-._~"),
            quote(self.project, safe="-._~"),
            quote(self.application, safe="-._~@*"),
            quote(self.agent, safe="-._~@*"),
        )
        return APPLICATION_RESOURCE_PREFIX + ":".join(encoded)

    def matches(self, target: "ApplicationResource") -> bool:
        if not target.exact:
            raise ApplicationResourceError("application_target_not_exact")
        return (
            self.tenant == target.tenant
            and self.project == target.project
            and self.application in ("*", target.application)
            and self.agent in ("*", target.agent)
        )

    def intersection(
        self,
        other: "ApplicationResource",
    ) -> "ApplicationResource | None":
        if self.tenant != other.tenant or self.project != other.project:
            return None

        def component(left: str, right: str) -> str | None:
            if left == right:
                return left
            if left == "*":
                return right
            if right == "*":
                return left
            return None

        application = component(self.application, other.application)
        agent = component(self.agent, other.agent)
        if application is None or agent is None:
            return None
        return ApplicationResource(
            tenant=self.tenant,
            project=self.project,
            application=application,
            agent=agent,
        )


def application_resource(
    *,
    tenant: str,
    project: str,
    application: str,
    agent: str,
) -> str:
    return ApplicationResource(
        tenant=tenant,
        project=project,
        application=application,
        agent=agent,
    ).resource


def validate_application_card_resource(
    resource: str,
    *,
    tenant: str,
    project: str,
    exact: bool = False,
) -> ApplicationResource | None:
    text = str(resource or "").strip()
    if not text.startswith(APPLICATION_RESOURCE_PREFIX):
        return None
    parsed = ApplicationResource.parse(text)
    if parsed.tenant != tenant or parsed.project != project:
        raise ApplicationResourceError("application_resource_deployment_mismatch")
    if exact and not parsed.exact:
        raise ApplicationResourceError("application_resource_not_exact")
    return parsed


__all__ = [
    "APPLICATION_RESOURCE_PREFIX",
    "APPLICATION_RESOURCE_SELECTOR_TYPE",
    "ApplicationResource",
    "ApplicationResourceError",
    "application_resource",
    "validate_application_card_resource",
]
