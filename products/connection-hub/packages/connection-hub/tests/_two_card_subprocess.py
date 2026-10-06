"""Fresh-process reader for the two-Card transaction regressions.

The parent passes only synthetic Card coordinates and a temporary storage root.
The process intentionally imports Connection Hub anew so a result cannot come
from a test's in-memory store, cache, or pending asyncio task.
"""

from __future__ import annotations

import asyncio
import json
import sys

from connection_hub.delegated_credentials.cards.store import (
    BundleStorageDelegatedCardStore,
)


async def _snapshot(request: dict) -> dict:
    store = BundleStorageDelegatedCardStore(request["storage_root"])
    cards = []
    for target in request["targets"]:
        loaded = await store.read_current_authority(
            subject_hash=target["subject_hash"], access_id=target["access_id"]
        )
        if loaded is None:
            cards.append(None)
            continue
        pointer, authority = loaded
        cards.append(
            {
                "access_id": authority.access_id,
                "card_revision": authority.card_revision,
                "content_hash": authority.content_hash(),
                "pointer_hash": pointer.content_hash,
                "state": authority.state,
                "issuer_kind": authority.issuer_kind,
                "issuer_ref": authority.issuer_ref,
            }
        )
    return {"cards": cards}


def main() -> None:
    request = json.load(sys.stdin)
    if request.get("action") != "snapshot":
        raise ValueError("subprocess_action_unknown")
    json.dump(asyncio.run(_snapshot(request)), sys.stdout, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
