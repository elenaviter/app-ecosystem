# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Durable delegated identity-card models and storage contracts."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_BASE = "connection_hub.delegated_credentials.cards"
_EXPORTS = {
    "CARD_STATE_ACTIVE": f"{_BASE}.model",
    "CARD_STATE_REVOKED": f"{_BASE}.model",
    "CardAuthority": f"{_BASE}.model",
    "CardCredentialHandles": f"{_BASE}.model",
    "CardCurrentPointer": f"{_BASE}.model",
    "CardRecordError": f"{_BASE}.model",
    "NamedServiceSelection": f"{_BASE}.model",
    "card_revision_name": f"{_BASE}.model",
    "ResidentCallerProfile": f"{_BASE}.identity",
    "is_resident_client_id": f"{_BASE}.identity",
    "legacy_resident_access_id": f"{_BASE}.identity",
    "resident_client_id": f"{_BASE}.identity",
    "stable_resident_access_id": f"{_BASE}.identity",
    "CardCacheEntry": f"{_BASE}.cache",
    "DelegatedCardRuntimeCache": f"{_BASE}.cache",
    "CardUnavailable": f"{_BASE}.resolver",
    "DelegatedCardResolver": f"{_BASE}.resolver",
    "CardCommitFailed": f"{_BASE}.service",
    "CardConflict": f"{_BASE}.service",
    "CardMutationLock": f"{_BASE}.service",
    "CardMutationLockTimeout": f"{_BASE}.service",
    "CardServingUnavailable": f"{_BASE}.service",
    "DelegatedCardService": f"{_BASE}.service",
    "CardHandleMetadata": f"{_BASE}.handle_metadata",
    "CardHandleMetadataConflict": f"{_BASE}.handle_metadata",
    "CardHandleMetadataStore": f"{_BASE}.handle_metadata",
    "PostgresCardHandleMetadataStore": f"{_BASE}.handle_authority",
    "ResidentCardSecretService": f"{_BASE}.resident_secrets",
    "ResidentSecretCleanupFailure": f"{_BASE}.resident_secrets",
    "ResidentSecretCleanupResult": f"{_BASE}.resident_secrets",
    "ResidentSecretEnvelope": f"{_BASE}.resident_secrets",
    "ResidentSecretError": f"{_BASE}.resident_secrets",
    "ResidentSecretInstallResult": f"{_BASE}.resident_secrets",
    "ResidentSecretRetirementResult": f"{_BASE}.resident_secrets",
    "ResidentSecretStore": f"{_BASE}.resident_secrets",
    "resident_bearer_fingerprint": f"{_BASE}.resident_secrets",
    "CardPersistence": f"{_BASE}.persistence",
    "DurableCardPersistence": f"{_BASE}.persistence",
    "LoadedCard": f"{_BASE}.persistence",
    "BundleStorageDelegatedCardStore": f"{_BASE}.store",
    "CardStorageError": f"{_BASE}.store",
    "DelegatedCardStore": f"{_BASE}.store",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if not module_name:
        raise AttributeError(name)
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
