# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""User-owned remote A2A connectors: registry, Hub-held credential, drift, tasks."""

from connection_hub.remote_a2a.catalog import (
    REMOTE_A2A_RESOURCE_KIND,
    RemoteA2AResourceRow,
    remote_a2a_resource_row,
    remote_a2a_resource_rows,
)
from connection_hub.remote_a2a.models import (
    EXTERNAL_A2A_GRANT,
    RESOURCE_PREFIX,
    RemoteA2AConnector,
    RemoteA2ARecordError,
    RemoteA2ATaskRecord,
    connector_id_from_resource,
    connector_resource,
)
from connection_hub.remote_a2a.service import (
    RemoteA2AConnectorConflict,
    RemoteA2AConnectorNotFound,
    RemoteA2AConnectorService,
    RemoteA2ATaskNotFound,
)
from connection_hub.remote_a2a.store import BundleStorageRemoteA2AConnectorStore

__all__ = [
    "BundleStorageRemoteA2AConnectorStore",
    "EXTERNAL_A2A_GRANT",
    "REMOTE_A2A_RESOURCE_KIND",
    "RESOURCE_PREFIX",
    "RemoteA2AConnector",
    "RemoteA2AConnectorConflict",
    "RemoteA2AConnectorNotFound",
    "RemoteA2AConnectorService",
    "RemoteA2ARecordError",
    "RemoteA2AResourceRow",
    "RemoteA2ATaskNotFound",
    "RemoteA2ATaskRecord",
    "connector_id_from_resource",
    "connector_resource",
    "remote_a2a_resource_row",
    "remote_a2a_resource_rows",
]
