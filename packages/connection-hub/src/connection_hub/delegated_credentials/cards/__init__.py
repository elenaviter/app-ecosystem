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
