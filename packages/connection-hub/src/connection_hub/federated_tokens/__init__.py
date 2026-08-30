# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Short-lived federated credentials for Connection Hub transport integrations."""

from connection_hub.federated_tokens.data_bus import (
    FEDERATED_TOKEN_DEFAULT_TTL_SECONDS,
    FEDERATED_TOKEN_MAX_TTL_SECONDS,
    FEDERATED_TOKEN_SCHEMA,
    FEDERATED_TOKEN_SECRET_KEY,
    FederatedTokenError,
    FederatedTokenExpired,
    FederatedTokenGrant,
    FederatedTokenInvalid,
    FederatedTokenVerification,
    issue_federated_data_bus_token,
    verify_federated_data_bus_token,
)

__all__ = [
    "FEDERATED_TOKEN_DEFAULT_TTL_SECONDS",
    "FEDERATED_TOKEN_MAX_TTL_SECONDS",
    "FEDERATED_TOKEN_SCHEMA",
    "FEDERATED_TOKEN_SECRET_KEY",
    "FederatedTokenError",
    "FederatedTokenExpired",
    "FederatedTokenGrant",
    "FederatedTokenInvalid",
    "FederatedTokenVerification",
    "issue_federated_data_bus_token",
    "verify_federated_data_bus_token",
]
