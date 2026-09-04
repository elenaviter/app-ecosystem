# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Deterministic resource-kind ownership for delegated MCP providers."""

from __future__ import annotations

from collections.abc import Iterable

from connection_hub.delegated_gateway.models import (
    DelegatedGatewayError,
    DelegatedResourceEntry,
    GatewayContractError,
)
from connection_hub.delegated_gateway.ports import DelegatedMCPResourceProvider


class DelegatedMCPProviderRegistry:
    def __init__(self, providers: Iterable[DelegatedMCPResourceProvider] = ()) -> None:
        self._providers: dict[str, DelegatedMCPResourceProvider] = {}
        self._by_kind: dict[str, DelegatedMCPResourceProvider] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: DelegatedMCPResourceProvider) -> None:
        provider_id = str(provider.provider_id or "").strip().lower()
        if not provider_id:
            raise GatewayContractError("provider_id_missing")
        if provider_id in self._providers:
            raise GatewayContractError("provider_id_duplicate")
        kinds = frozenset(
            str(kind or "").strip().lower() for kind in provider.resource_kinds
        )
        if not kinds or "" in kinds:
            raise GatewayContractError("provider_resource_kinds_invalid")
        for kind in kinds:
            if kind in self._by_kind:
                raise GatewayContractError("provider_kind_ambiguous")
        self._providers[provider_id] = provider
        for kind in kinds:
            self._by_kind[kind] = provider

    def provider_for(
        self, resource: DelegatedResourceEntry
    ) -> DelegatedMCPResourceProvider:
        provider = self._by_kind.get(resource.kind)
        if provider is None:
            raise DelegatedGatewayError(
                "resource_provider_not_found", resource_id=resource.resource_id
            )
        if (
            resource.provider_id
            and resource.provider_id != str(provider.provider_id).strip().lower()
        ):
            raise DelegatedGatewayError(
                "resource_provider_mismatch", resource_id=resource.resource_id
            )
        return provider

    def provider_id_for(self, resource: DelegatedResourceEntry) -> str:
        return str(self.provider_for(resource).provider_id)

    @property
    def provider_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))


__all__ = ["DelegatedMCPProviderRegistry"]
