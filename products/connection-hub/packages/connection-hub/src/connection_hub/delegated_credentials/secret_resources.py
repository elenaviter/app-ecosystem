# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Typed KDCube secret resources used by delegated Cards.

The package owns authority syntax without importing KDCube. Exact execution
targets and standing selectors share one URN shape; selectors are constrained
to a trailing prefix wildcard and exact deployment coordinates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, unquote

SECRET_RESOURCE_PREFIX = "urn:kdcube:management:secret:"
SECRET_SELECTOR_TYPE = "kdcube_secret"
SECRET_SCOPES = frozenset({"platform", "bundle", "user"})

_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@-]{0,511}$")


class SecretResourceError(ValueError):
    pass


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
        raise SecretResourceError(f"{name}_invalid")
    return text


def _key_pattern(value: str, *, scope: str) -> str:
    text = str(value or "").strip()
    broad = text == "*" or text.endswith(".*")
    exact = text[:-1] if broad and text != "*" else text
    if (
        not text
        or text.count("*") > 1
        or ("*" in text and not broad)
        or any(marker in text for marker in ("?", "[", "]"))
        or ".." in exact
        or (exact != "*" and not _COMPONENT_RE.fullmatch(exact.rstrip(".")))
    ):
        raise SecretResourceError("secret_key_selector_invalid")
    if scope == "platform":
        if text == "*" or not text.startswith("platform."):
            raise SecretResourceError("platform_secret_selector_invalid")
    elif text.startswith(("platform.", "bundles.", "users.")):
        raise SecretResourceError("relative_secret_selector_invalid")
    return text


@dataclass(frozen=True)
class SecretResource:
    tenant: str
    project: str
    scope: str
    scope_id: str
    key: str

    def __post_init__(self) -> None:
        tenant = _component(self.tenant, name="tenant")
        project = _component(self.project, name="project")
        scope = str(self.scope or "").strip().lower()
        if scope not in SECRET_SCOPES:
            raise SecretResourceError("secret_scope_invalid")
        if scope == "platform":
            if self.scope_id != "_":
                raise SecretResourceError("platform_scope_id_invalid")
            scope_id = "_"
        elif scope == "bundle":
            scope_id = _component(
                self.scope_id,
                name="bundle_scope_id",
                allow_star=True,
            )
        else:
            scope_id = str(self.scope_id or "").strip()
            if "~" in scope_id:
                if scope_id.count("~") != 1:
                    raise SecretResourceError("user_scope_id_invalid")
                user_id, bundle_id = scope_id.split("~", 1)
                _component(user_id, name="user_scope_id")
                _component(bundle_id, name="user_bundle_scope_id")
            else:
                scope_id = _component(
                    scope_id,
                    name="user_scope_id",
                    allow_star=True,
                )
        key = _key_pattern(self.key, scope=scope)
        object.__setattr__(self, "tenant", tenant)
        object.__setattr__(self, "project", project)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "scope_id", scope_id)
        object.__setattr__(self, "key", key)

    @classmethod
    def parse(cls, value: str) -> "SecretResource":
        text = str(value or "").strip()
        if not text.startswith(SECRET_RESOURCE_PREFIX):
            raise SecretResourceError("secret_resource_invalid")
        segments = text[len(SECRET_RESOURCE_PREFIX) :].split(":")
        if len(segments) != 5:
            raise SecretResourceError("secret_resource_invalid")
        try:
            decoded = [unquote(segment, errors="strict") for segment in segments]
        except UnicodeError as exc:
            raise SecretResourceError("secret_resource_invalid") from exc
        resource = cls(*decoded)
        if resource.resource != text:
            raise SecretResourceError("secret_resource_not_canonical")
        return resource

    @property
    def broad(self) -> bool:
        return self.scope_id == "*" or "*" in self.key

    @property
    def resource(self) -> str:
        encoded = (
            quote(self.tenant, safe="-._~"),
            quote(self.project, safe="-._~"),
            quote(self.scope, safe="-._~"),
            quote(self.scope_id, safe="-._~@*"),
            quote(self.key, safe="-._~@*"),
        )
        return SECRET_RESOURCE_PREFIX + ":".join(encoded)


def validate_secret_card_resource(
    resource: str,
    *,
    tenant: str,
    project: str,
) -> SecretResource | None:
    text = str(resource or "").strip()
    if not text.startswith(SECRET_RESOURCE_PREFIX):
        return None
    parsed = SecretResource.parse(text)
    if parsed.tenant != tenant or parsed.project != project:
        raise SecretResourceError("secret_resource_deployment_mismatch")
    return parsed


def secret_resource(
    *,
    tenant: str,
    project: str,
    scope: str,
    scope_id: str,
    key: str,
) -> str:
    return SecretResource(
        tenant=tenant,
        project=project,
        scope=scope,
        scope_id=scope_id,
        key=key,
    ).resource


def whole_deployment_secret_resources(*, tenant: str, project: str) -> tuple[str, ...]:
    return (
        secret_resource(
            tenant=tenant,
            project=project,
            scope="platform",
            scope_id="_",
            key="platform.*",
        ),
        secret_resource(
            tenant=tenant,
            project=project,
            scope="bundle",
            scope_id="*",
            key="*",
        ),
        secret_resource(
            tenant=tenant,
            project=project,
            scope="user",
            scope_id="*",
            key="*",
        ),
    )


__all__ = [
    "SECRET_RESOURCE_PREFIX",
    "SECRET_SCOPES",
    "SECRET_SELECTOR_TYPE",
    "SecretResource",
    "SecretResourceError",
    "secret_resource",
    "validate_secret_card_resource",
    "whole_deployment_secret_resources",
]
