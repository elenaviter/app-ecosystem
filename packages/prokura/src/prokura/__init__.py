"""Prokura: one authority for delegated access.

Prokura is an old commercial-law institution: delegated signing authority
that lives in a register, verified against the register rather than any
carried letter, revocable at the register. This package is that idea,
built for agents and automations: every caller has its own identity card,
the authority lives in one central record, and a guarded boundary resolves
the current authority on every call.

Version 0.0.2 is a development release. It contains the host-neutral authority
core, delegated card/catalog contracts, OAuth and connected-account policy,
named-service admission, and direct protected-service admission. Host storage,
identity, and transport adapters remain explicit integration responsibilities.
See the repository for status: https://github.com/elenaviter/app-ecosystem
"""

from importlib import import_module
from typing import Any

__version__ = "0.0.2"

_EXPORTS: dict[str, str] = {}
for name in (
    "NAMESPACE",
    "CONNECTION_CATALOG",
    "CONNECTION_STATUS",
    "CONNECTION_GET_TOKEN",
    "CONNECTION_DISCONNECT",
    "OAUTH_START",
    "AGENT_GRANT_GET_TOKEN",
    "AGENT_GRANT_CHECK",
    "CONNECTION_OPERATIONS",
    "ConnectionOperationSpec",
    "build_connection_operations",
    "Connection",
    "ConnectionToken",
    "CatalogEntry",
    "ClientApp",
    "AmbiguousConnectionAccount",
):
    _EXPORTS[name] = "prokura.contract"
for name in ("ConnectionsClient", "ConnectionsError", "ConnectionsTransport"):
    _EXPORTS[name] = "prokura.client"
for name in (
    "AuthorityProviderSpec",
    "AuthorityRegistry",
    "AuthorityResolution",
    "CredentialEnvelope",
    "authority_provider_spec_from_declaration",
):
    _EXPORTS[name] = "prokura.authority_registry"

__all__ = ["__version__", *_EXPORTS]


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if not module_name:
        raise AttributeError(name)
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
