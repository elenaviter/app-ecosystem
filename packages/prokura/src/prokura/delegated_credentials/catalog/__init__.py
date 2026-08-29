# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Versioned delegated-service catalog: source, documents, and durable storage."""

from prokura.delegated_credentials.catalog.hashing import (
    connections_content_hash,
)
from prokura.delegated_credentials.catalog.models import (
    CatalogDocument,
    CatalogDocumentError,
    catalog_version_name,
)
from prokura.delegated_credentials.catalog.publisher import (
    CatalogPublicationError,
    CatalogPublicationResult,
    SharedStorageOperationRunner,
    ensure_delegated_catalog,
)
from prokura.delegated_credentials.catalog.resolver import (
    CatalogUnavailable,
    DelegatedCatalogResolver,
)
from prokura.delegated_credentials.catalog.runtime_cache import (
    DelegatedCatalogRuntimeCache,
)
from prokura.delegated_credentials.catalog.source import (
    connections_from_props,
)
from prokura.delegated_credentials.catalog.store import (
    BundleStorageDelegatedCatalogStore,
    CatalogStorageError,
    DelegatedCatalogStore,
)

__all__ = [
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
