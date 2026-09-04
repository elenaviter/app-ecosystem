# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Versioned delegated-service catalog: source, documents, and durable storage."""

from connection_hub.delegated_credentials.catalog.descriptors import (
    RESOURCE_KIND_CATALOG,
    RESOURCE_KIND_REMOTE_MCP,
    ResourceAcceptance,
    ResourceAcceptanceError,
    next_resource_acceptance,
    operation_descriptor_digest,
    resource_descriptor_state,
    resource_row_digest,
    row_acceptance,
)
from connection_hub.delegated_credentials.catalog.hashing import (
    connections_content_hash,
)
from connection_hub.delegated_credentials.catalog.models import (
    CatalogDocument,
    CatalogDocumentError,
    catalog_version_name,
)
from connection_hub.delegated_credentials.catalog.publisher import (
    CatalogPublicationError,
    CatalogPublicationResult,
    SharedStorageOperationRunner,
    ensure_delegated_catalog,
)
from connection_hub.delegated_credentials.catalog.resolver import (
    CatalogUnavailable,
    DelegatedCatalogResolver,
)
from connection_hub.delegated_credentials.catalog.runtime_cache import (
    DelegatedCatalogRuntimeCache,
)
from connection_hub.delegated_credentials.catalog.source import (
    connections_from_props,
)
from connection_hub.delegated_credentials.catalog.store import (
    BundleStorageDelegatedCatalogStore,
    CatalogStorageError,
    DelegatedCatalogStore,
)

__all__ = [
    "RESOURCE_KIND_CATALOG",
    "RESOURCE_KIND_REMOTE_MCP",
    "ResourceAcceptance",
    "ResourceAcceptanceError",
    "next_resource_acceptance",
    "operation_descriptor_digest",
    "resource_descriptor_state",
    "resource_row_digest",
    "row_acceptance",
    "BundleStorageDelegatedCatalogStore",
    "CatalogDocument",
    "CatalogDocumentError",
    "CatalogStorageError",
    "CatalogUnavailable",
    "DelegatedCatalogResolver",
    "DelegatedCatalogRuntimeCache",
    "DelegatedCatalogStore",
    "CatalogPublicationError",
    "CatalogPublicationResult",
    "SharedStorageOperationRunner",
    "catalog_version_name",
    "connections_content_hash",
    "connections_from_props",
    "ensure_delegated_catalog",
]
