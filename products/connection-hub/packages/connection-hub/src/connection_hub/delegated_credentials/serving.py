# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Readers of current delegated authority, composed for a serving process.

A governed request resolves current authority from two shared sources: the
registered catalog and durable card history. The process that enforces them
does not own them, so the platform composes the readers once and request paths
consume them. Enforcement code never learns which bundle publishes the catalog
or how its storage is addressed.

Resolution order for a consumer is request state, then app state. Absent
readers are not "no requirement": the caller must fail closed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from connection_hub.delegated_credentials.cache_settings import (
    DelegatedCacheSettings,
)
from connection_hub.delegated_credentials.cards.store import (
    BundleStorageDelegatedCardStore,
)
from connection_hub.delegated_credentials.catalog.resolver import (
    DelegatedCatalogResolver,
)
from connection_hub.delegated_credentials.catalog.runtime_cache import (
    DelegatedCatalogRuntimeCache,
)
from connection_hub.delegated_credentials.catalog.store import (
    BundleStorageDelegatedCatalogStore,
)

LOGGER = logging.getLogger("connection_hub.delegated_serving")

SERVING_RESOLVERS_ATTR = "delegated_serving_resolvers"

ConnectionHubAppIdResolver = Callable[[], str]
StorageRootResolver = Callable[..., Any]
BundlePropsLoader = Callable[..., Awaitable[Mapping[str, Any] | None]]


@dataclass(frozen=True)
class DelegatedServingResolvers:
    """Read-only resolution of current delegated authority."""

    catalog: DelegatedCatalogResolver
    cards: BundleStorageDelegatedCardStore


def connection_hub_app_id(
    resolver: ConnectionHubAppIdResolver | None = None,
) -> str:
    """The application that publishes delegated catalog and card history."""
    return str(resolver() if resolver is not None else "").strip()


async def build_delegated_serving_resolvers(
    *,
    redis: Any,
    tenant: str,
    project: str,
    bundle_id: str = "",
    app_id_resolver: ConnectionHubAppIdResolver | None = None,
    storage_root_resolver: StorageRootResolver | None = None,
    bundle_props_loader: BundlePropsLoader | None = None,
) -> DelegatedServingResolvers | None:
    """Compose the readers over the publishing bundle's shared storage.

    Returns ``None`` when Redis or that storage cannot be addressed, so the
    caller installs nothing rather than a half-built reader.
    """
    if redis is None:
        return None
    app_id = str(bundle_id or "").strip() or connection_hub_app_id(app_id_resolver)
    scope_tenant = str(tenant or "").strip()
    scope_project = str(project or "").strip()
    if not app_id or not scope_tenant or not scope_project:
        return None

    if storage_root_resolver is None:
        LOGGER.warning(
            "[connection-hub.delegated-serving] storage-root resolver is not configured"
        )
        return None
    try:
        storage_root = storage_root_resolver(
            bundle_id=app_id, tenant=scope_tenant, project=scope_project, ensure=False
        )
    except Exception:
        LOGGER.warning(
            "[connection-hub.delegated-serving] storage root unavailable bundle=%s", app_id,
            exc_info=True,
        )
        return None

    settings = await _cache_settings(
        tenant=scope_tenant,
        project=scope_project,
        bundle_id=app_id,
        bundle_props_loader=bundle_props_loader,
    )
    cache = DelegatedCatalogRuntimeCache(redis, tenant=scope_tenant, project=scope_project)
    return DelegatedServingResolvers(
        catalog=DelegatedCatalogResolver(
            cache=cache,
            store=BundleStorageDelegatedCatalogStore(storage_root),
            settings=settings,
        ),
        cards=BundleStorageDelegatedCardStore(storage_root),
    )


async def _cache_settings(
    *,
    tenant: str,
    project: str,
    bundle_id: str,
    bundle_props_loader: BundlePropsLoader | None = None,
) -> DelegatedCacheSettings:
    """Residency settings from the publishing bundle's descriptor.

    These are cache lifetimes, not authority, so an unreadable descriptor falls
    back to the defaults instead of refusing to serve.
    """
    if bundle_props_loader is None:
        return DelegatedCacheSettings()
    try:
        props = await bundle_props_loader(
            tenant=tenant, project=project, bundle_id=bundle_id
        )
        connections = (props or {}).get("connections") if isinstance(props, dict) else None
        return DelegatedCacheSettings.from_connections(connections or {})
    except Exception:
        LOGGER.warning(
            "[connection-hub.delegated-serving] descriptor residency unavailable bundle=%s;"
            " using defaults",
            bundle_id,
            exc_info=True,
        )
        return DelegatedCacheSettings()


async def install_delegated_serving_resolvers(
    app: Any,
    *,
    redis: Any,
    tenant: str,
    project: str,
    app_id_resolver: ConnectionHubAppIdResolver | None = None,
    storage_root_resolver: StorageRootResolver | None = None,
    bundle_props_loader: BundlePropsLoader | None = None,
) -> bool:
    """Bind the readers onto an application for its whole lifetime.

    One process serves one tenant/project, so the readers have no per-request
    input and are composed once at startup.
    """
    resolvers = await build_delegated_serving_resolvers(
        redis=redis,
        tenant=tenant,
        project=project,
        app_id_resolver=app_id_resolver,
        storage_root_resolver=storage_root_resolver,
        bundle_props_loader=bundle_props_loader,
    )
    state = getattr(app, "state", None)
    if resolvers is None or state is None:
        LOGGER.warning(
            "[connection-hub.delegated-serving] readers not installed tenant=%s project=%s",
            tenant,
            project,
        )
        return False
    setattr(state, SERVING_RESOLVERS_ATTR, resolvers)
    LOGGER.info(
        "[connection-hub.delegated-serving] readers installed tenant=%s project=%s bundle=%s",
        tenant,
        project,
        connection_hub_app_id(app_id_resolver),
    )
    return True


def delegated_serving_resolvers(request: Any) -> DelegatedServingResolvers | None:
    """Request-scoped readers first, then the application's own."""
    for holder in (getattr(request, "state", None), getattr(getattr(request, "app", None), "state", None)):
        resolvers = getattr(holder, SERVING_RESOLVERS_ATTR, None) if holder is not None else None
        if isinstance(resolvers, DelegatedServingResolvers):
            return resolvers
    return None


__all__ = [
    "SERVING_RESOLVERS_ATTR",
    "BundlePropsLoader",
    "ConnectionHubAppIdResolver",
    "DelegatedServingResolvers",
    "StorageRootResolver",
    "build_delegated_serving_resolvers",
    "connection_hub_app_id",
    "delegated_serving_resolvers",
    "install_delegated_serving_resolvers",
]
