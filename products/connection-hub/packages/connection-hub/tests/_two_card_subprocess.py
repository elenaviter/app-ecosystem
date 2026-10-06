"""Fresh-process reader for the two-Card transaction regressions.

The parent passes only synthetic Card coordinates and a temporary storage root.
The process intentionally imports Connection Hub anew so a result cannot come
from a test's in-memory store, cache, or pending asyncio task.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from datetime import datetime

from connection_hub.delegated_credentials.cards import lifecycle_store
from connection_hub.delegated_credentials.cards.lifecycle import LifecycleRequest
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


async def _transaction(request: dict) -> dict:
    store = BundleStorageDelegatedCardStore(request["storage_root"])
    lifecycle_request = LifecycleRequest.from_mapping(request["body"])

    async def before_publish() -> None:
        if request["action"] == "replay":
            raise AssertionError("durable replay must not publish twice")

    if request["action"] in ("fault_prepare", "fault_first_pointer", "fault_commit"):
        original_write = lifecycle_store.write_json_atomic
        pending_writes = 0

        async def kill_after_stage(path, payload) -> None:
            nonlocal pending_writes
            await original_write(path, payload)
            if payload.get("schema") == lifecycle_store.LIFECYCLE_POINTER_SCHEMA:
                pending_writes += 1
                if request["action"] == "fault_first_pointer" and pending_writes == 1:
                    os.kill(os.getpid(), signal.SIGKILL)
            if (
                payload.get("schema") == lifecycle_store.LIFECYCLE_RECEIPT_SCHEMA
                and payload.get("state")
                == ("prepared" if request["action"] == "fault_prepare" else "committed")
            ):
                os.kill(os.getpid(), signal.SIGKILL)

        lifecycle_store.write_json_atomic = kill_after_stage

    return await lifecycle_store.atomic_revoke(
        store,
        request=lifecycle_request,
        actor_subject=request["actor_subject"],
        now=datetime.fromisoformat(request["now"]),
        before_publish=before_publish,
    )


def main() -> None:
    request = json.load(sys.stdin)
    action = request.get("action")
    if action == "snapshot":
        result = asyncio.run(_snapshot(request))
    elif action in ("fault_prepare", "fault_first_pointer", "fault_commit", "replay"):
        result = asyncio.run(_transaction(request))
    else:
        raise ValueError("subprocess_action_unknown")
    json.dump(result, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
