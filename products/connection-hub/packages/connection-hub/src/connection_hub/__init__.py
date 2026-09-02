"""Connection Hub delegated-access authority and client SDK.

Every caller has its own identity card, authority lives in one central record,
and a guarded boundary resolves current authority on every call.

This is an alpha release. It contains the host-neutral authority
core, delegated card/catalog contracts, OAuth and connected-account policy,
named-service admission, and direct protected-service admission. Host storage,
identity, and transport adapters remain explicit integration responsibilities.
See the repository for status: https://github.com/elenaviter/app-ecosystem
"""

from importlib import import_module
from typing import Any

__version__ = "2026.09.02.1410"

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
    _EXPORTS[name] = "connection_hub.contract"
for name in ("ConnectionsClient", "ConnectionsError", "ConnectionsTransport"):
    _EXPORTS[name] = "connection_hub.client"
for name in (
    "AuthorityProviderSpec",
    "AuthorityRegistry",
    "AuthorityResolution",
    "CredentialEnvelope",
    "authority_provider_spec_from_declaration",
):
    _EXPORTS[name] = "connection_hub.authority_registry"

__all__ = ["__version__", *_EXPORTS]


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if not module_name:
        raise AttributeError(name)
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
