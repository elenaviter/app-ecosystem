"""The one transport identity of a governed request (W574).

The board's per-operation receipts hash server-derived inputs and never return
them, so a client whose reply was lost could not name its request to the
board. This hash is computed from exactly what the client sends - the action,
the object ref and the payload - and the board's transport ledger records the
same value at admission. Client and board must use this function, byte for
byte: canonical JSON with sorted keys, no spaces, non-ASCII kept as UTF-8
(ensure_ascii=False), then sha256 hex. Cross-implementation vectors in
tests/fixtures/transport_request_hash_vectors.json pin it, non-ASCII included
(W502 lesson: two JSON digests with different Unicode contracts disagree).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

TRANSPORT_REQUEST_HASH_SCHEMA = "problem-board.transport-request-hash.v1"

# The answers operation.receipt.get gives (W574, Ops conditions 2 and 3).
RECEIPT_STATES = ("applied", "refused", "in_progress", "no_record")


def transport_request_hash(action: str, object_ref: str, payload: Mapping[str, Any]) -> str:
    """sha256 hex of the canonical request: action, object ref and payload."""

    encoded = json.dumps(
        {"action": str(action), "object_ref": str(object_ref), "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = ["RECEIPT_STATES", "TRANSPORT_REQUEST_HASH_SCHEMA", "transport_request_hash"]
