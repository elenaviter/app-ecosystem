# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Network boundary for user-supplied remote MCP endpoints.

The policy is App Foundation's single outbound endpoint policy
(``app_foundation.net``); these names keep the remote MCP API unchanged.
"""

from __future__ import annotations

from app_foundation.net import (
    AddressResolver,
    OutboundEndpointDenied as RemoteMCPEndpointDenied,
    OutboundEndpointPolicy as RemoteMCPEndpointPolicy,
    resolve_endpoint_addresses,
)

__all__ = [
    "AddressResolver",
    "RemoteMCPEndpointDenied",
    "RemoteMCPEndpointPolicy",
    "resolve_endpoint_addresses",
]
