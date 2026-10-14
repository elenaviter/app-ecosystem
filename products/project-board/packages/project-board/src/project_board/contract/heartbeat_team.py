"""Display-only heartbeat TEAM v1 wire identities; never action authority."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any


TEAM_PROTOCOL = "problem_board.heartbeat_team.v1"
REVISION_PREFIX = "team-v1"
_REVISION = re.compile(r"team-v1:([0-9a-f]{64}):([0-9a-f]{64})\Z")


def content_digest(value: Any) -> str:
    """Preserve all fields, ordered lists, empty values and observation evidence."""
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def revision_parts(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, str) or len(value) != 137:
        return None
    match = _REVISION.fullmatch(value)
    return (match[1], match[2]) if match else None
