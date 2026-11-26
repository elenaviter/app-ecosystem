# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""The slice half of a consent given from a conversation.

Consent writes the Agent Card (the new default) and that conversation's slice
(this conversation's selection) in one Card transaction: the slice change is
the ``conversation_slice`` effect of the Card's write. The granted categories
are added to the conversation's current selection; nothing else changes.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Mapping

from connection_hub.delegated_credentials.slices.store import (
    SELECTION_CATEGORIES, SLICE_STATE_DELETING, BundleStorageConversationSliceStore, SliceStoreError,
)


def _union(current: Any, granted: Any) -> Any:
    if isinstance(current, Mapping) or isinstance(granted, Mapping):
        merged = {key: value for key, value in dict(current or {}).items()}
        for key, value in dict(granted or {}).items():
            merged[key] = _union(merged.get(key), value)
        return merged
    return sorted(set(current or ()) | set(granted or ()))


async def conversation_slice_effect(
    store: BundleStorageConversationSliceStore, *, card: Any, conversation_id: str, granted: Mapping[str, Any],
) -> dict[str, Any]:
    """The effect that adds ``granted`` to the conversation's slice, pinned to its current revision."""

    unknown = set(granted) - set(SELECTION_CATEGORIES)
    if unknown:
        raise SliceStoreError("conversation_slice_category_not_guarded")
    current = await store.read(grantor_subject=card.grantor_subject, card_access_id=card.access_id,
                               conversation_id=conversation_id)
    if current is None:
        # A conversation's consent follows its turn token, which seeded the slice.
        raise SliceStoreError("conversation_slice_missing", retryable=True)
    if current.state == SLICE_STATE_DELETING:
        raise SliceStoreError("conversation_deleting")
    selection = {category: _union(current.selection.get(category), granted.get(category))
                 for category in SELECTION_CATEGORIES if category in current.selection or category in granted}
    candidate = dataclasses.replace(current, selection=selection)
    return {"kind": "conversation_slice", "key": "conversation-slice",
            "payload": {"access_id": card.access_id, "owner_subject": card.grantor_subject,
                        "expected_revision": current.slice_revision, "slice": candidate.to_document()}}


__all__ = ["conversation_slice_effect"]
