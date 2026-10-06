"""Descriptor-selected, authenticated issuer ports. No issuer policy lives here."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from connection_hub.bundle_operations import normalize_bundle_operation_result
from connection_hub.delegated_credentials.admission import MIN_SERVICE_SECRET_BYTES
from connection_hub.delegated_credentials.agent_capability_control import AGENT_DESCRIPTOR_ISSUER_KIND
from connection_hub.delegated_credentials.issuer_gate import IssuerRegistry, IssuerWriteRefused
from connection_hub.delegated_credentials.issuer_read import (
    IssuerReadRegistry, IssuerReadRefused, RemoteIssuerReadAdapter,
    issuer_read_request_from_mapping, sign_issuer_read_request,
)
from connection_hub.delegated_credentials.remote_issuer import (
    RemoteIssuerAdapter, issuer_request_from_mapping,
    sign_issuer_envelope, sign_issuer_request,
)

SecretResolver = Callable[[str], Awaitable[str]]
BundleCaller = Callable[..., Awaitable[Mapping[str, Any]]]


class _UnavailableAuthority:
    def __init__(self, issuer_kind: str) -> None:
        self.issuer_kind = issuer_kind
        self.adapter_id = "invalid-config"

    async def prepare_context(self, request, **kwargs):
        raise IssuerWriteRefused("issuer_authority_config_invalid")

    async def decide(self, request):
        return False, "issuer_authority_config_invalid", "invalid-config", datetime.now(timezone.utc) + timedelta(seconds=1)


class _PeerTransport:
    def __init__(self, *, config: Mapping[str, str], resolve_secret: SecretResolver,
                 caller: BundleCaller) -> None:
        self._config = dict(config)
        self._resolve_secret = resolve_secret
        self._caller = caller

    async def _call(self, operation: str, *, protocol: str, payload: Mapping[str, Any]):
        if not operation:
            raise IssuerWriteRefused("issuer_context_provider_unavailable")
        try:
            secret = await self._resolve_secret(self._config["peer_proof_secret_ref"])
        except Exception as exc:
            raise IssuerWriteRefused("issuer_peer_proof_not_configured") from exc
        if not isinstance(secret, (str, bytes)) or len(secret.encode("utf-8") if isinstance(secret, str) else secret) < MIN_SERVICE_SECRET_BYTES:
            raise IssuerWriteRefused("issuer_peer_proof_not_configured")
        signing = dict(secret=secret, bundle_id=self._config["bundle_id"], operation=operation,
                       service_id=self._config["service_id"])
        if protocol == "issuer-decision.v1":
            body = sign_issuer_request(**signing, request=issuer_request_from_mapping(payload))
        elif protocol == "issuer-read.v1":
            body = sign_issuer_read_request(**signing,
                request=issuer_read_request_from_mapping(payload["request"]),
                phase=payload["phase"], snapshots=payload["snapshots"])
        else:
            body = sign_issuer_envelope(**signing, protocol=protocol, payload=payload)
        response = await self._caller(bundle_id=self._config["bundle_id"], operation=operation,
                                      data=body, route="public", http_method="POST")
        if not isinstance(response, Mapping):
            raise ValueError("issuer_provider_response_invalid")
        return normalize_bundle_operation_result(operation, response)

    async def decide(self, payload):
        return await self._call(self._config["operation"], protocol="issuer-decision.v1", payload=payload)

    async def prepare(self, payload):
        return await self._call(self._config.get("prepare_operation", ""), protocol="issuer-context.v1", payload=payload)

    async def finalize(self, payload):
        return await self._call(self._config.get("finalize_operation", ""), protocol="issuer-outcome.v1", payload=payload)

    async def read(self, payload):
        return await self._call(self._config.get("read_operation", ""), protocol="issuer-read.v1", payload=payload)


def issuer_read_registry_from_connections(*, connections: Mapping[str, Any],
        resolve_secret: SecretResolver, caller: BundleCaller) -> IssuerReadRegistry:
    """Optional read capability, never implied by a write operation/owner."""
    registry = IssuerReadRegistry()
    delegated = connections.get("delegated_credentials")
    rows = delegated.get("issuer_authorities") if isinstance(delegated, Mapping) else None
    if not isinstance(rows, Mapping):
        return registry
    required = ("bundle_id", "read_operation", "service_id", "peer_proof_secret_ref", "adapter_id")
    for kind, value in rows.items():
        if (type(kind) is not str or not kind.strip() or not isinstance(value, Mapping)
                or any(type(value.get(k)) is not str or not value[k].strip() for k in required)):
            continue  # every unregistered read kind refuses, including local owners
        paths = value.get("read_identity_leaf_paths", [])
        if (not isinstance(paths, list) or any(not isinstance(p, list) or not p
                or any(type(k) is not str for k in p) for p in paths)):
            continue
        config = {k: value[k].strip() for k in required}
        peer = _PeerTransport(config=config, resolve_secret=resolve_secret, caller=caller)
        registry.register(RemoteIssuerReadAdapter(issuer_kind=kind.strip(), adapter_id=config["adapter_id"],
            transport=peer.read, identity_leaf_paths=tuple(tuple(p) for p in paths)))
    return registry


def issuer_registry_from_connections(*, connections: Mapping[str, Any],
                                     resolve_secret: SecretResolver, caller: BundleCaller) -> IssuerRegistry:
    """Construct fresh request-local trusted adapters, never cached decisions.

    Operation strings and secret references come only from trusted descriptor
    configuration. Missing opaque issuers already refuse in IssuerRegistry;
    malformed declared rows also refuse, even for an owner-managed kind.
    """
    registry = IssuerRegistry(owner_managed_kinds=("application", AGENT_DESCRIPTOR_ISSUER_KIND))
    delegated = connections.get("delegated_credentials")
    raw = delegated.get("issuer_authorities") if isinstance(delegated, Mapping) else None
    if raw is None:
        return registry
    if not isinstance(raw, Mapping):
        # A malformed global configuration cannot silently restore exceptions.
        for kind in ("application", AGENT_DESCRIPTOR_ISSUER_KIND):
            registry.register(_UnavailableAuthority(kind))
        return registry
    required = ("bundle_id", "operation", "service_id", "peer_proof_secret_ref", "adapter_id")
    optional = ("prepare_operation", "finalize_operation")
    for kind, value in raw.items():
        if not isinstance(kind, str) or not kind.strip():
            continue
        kind = kind.strip()
        if (not isinstance(value, Mapping)
                or any(type(value.get(key)) is not str or not value[key].strip() for key in required)
                or any(key in value and type(value[key]) is not str for key in optional)):
            registry.register(_UnavailableAuthority(kind))
            continue
        config = {key: value[key].strip() for key in required + optional if key in value}
        peer = _PeerTransport(config=config, resolve_secret=resolve_secret, caller=caller)
        registry.register(RemoteIssuerAdapter(
            issuer_kind=kind, adapter_id=config["adapter_id"], transport=peer.decide,
            prepare_transport=peer.prepare if config.get("prepare_operation") else None,
            finalize_transport=peer.finalize if config.get("finalize_operation") else None,
        ))
    return registry
