"""Stateless, lossless heartbeat replies. Content identities are not authority.

Both peers use this module. A section body includes the observation layout;
changing that layout replaces the section rather than reusing old row indices.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
import re
from typing import Any, Mapping


REPLY_PROTOCOL = "problem_board.heartbeat_reply.v1"
MAX_KNOWN_BYTES = 32_768
_SHORT = re.compile(r"[A-Za-z0-9_-]{22}\Z")
_FULL = re.compile(r"[0-9a-f]{64}\Z")
_OBSERVATIONS = frozenset({
    "observed_at", "heartbeat_at", "updated_at", "store_reads_observed_at",
    "host_free_bytes", "reported_at", "runtime_account_observed_at",
    "last_inbox_check_at", "relay_heartbeat_at", "heartbeat_age_seconds",
    "relay_heartbeat_age_seconds",
})
# W827 S2b: request-side compaction a Board advertises in its reply marker.
# A relay uses a compact request form only after the Board's previous reply for
# that scope named the feature; an absent marker or feature means full forms.
KNOWN_DIGEST_FEATURE = "known_digest"
STORE_READS_CHANGED_FEATURE = "store_reads_changed"
SESSION_TIMES_FEATURE = "agent_session_times"
REQUEST_FEATURES = frozenset({KNOWN_DIGEST_FEATURE, STORE_READS_CHANGED_FEATURE, SESSION_TIMES_FEATURE})
# The session fields a times-only report may carry; any other change is full.
SESSION_TIME_FIELDS = ("heartbeat_at", "last_inbox_check_at")
# Only normalized store-read summaries at the three producer-owned paths.
# Other fields named ms/op (and summary counts, keys and ranges) stay body.
_STORE_READ_TELEMETRY = frozenset({"ms", "op"})


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value: Any, *, short: bool = False) -> str:
    raw = hashlib.sha256(canonical_bytes(value)).digest()
    return base64.urlsafe_b64encode(raw[:16]).decode("ascii").rstrip("=") if short else raw.hex()


def _pointer(path: tuple[str, ...]) -> str:
    return "/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in path)


def _split(value: Any, *, section: str = "") -> tuple[Any, dict[str, Any]]:
    observations: dict[str, Any] = {}
    read_root = value if section == "store_reads" else (
        value.get("store_reads") if section in {"self", "team"} and isinstance(value, dict) else None
    )

    def walk(node: Any, path: tuple[str, ...]) -> Any:
        if isinstance(node, dict):
            body = {}
            for key, child in node.items():
                if not isinstance(key, str):
                    raise ValueError("Heartbeat object keys must be strings")
                leaf = not isinstance(child, (dict, list))
                store_read_entry = isinstance(read_root, dict) and (
                    (section == "store_reads" and len(path) == 1) or (
                        section in {"self", "team"} and len(path) == 2 and path[0] == "store_reads"
                    )
                )
                observed = key in _OBSERVATIONS or (
                    key == "at" and (section == "store_reads" or "store_reads" in path)
                ) or (
                    key in _STORE_READ_TELEMETRY and store_read_entry
                )
                if observed and leaf:
                    observations[_pointer((*path, key))] = child
                    body[key] = None
                else:
                    body[key] = walk(child, (*path, key))
            return body
        if isinstance(node, list):
            return [walk(child, (*path, str(index))) for index, child in enumerate(node)]
        return node

    return walk(value, ()), observations


def _section(name: str, value: Any) -> tuple[Any, dict[str, dict[str, Any]]]:
    if name in _OBSERVATIONS and not isinstance(value, (dict, list)):
        return None, {"": {"": value}}
    if name == "team" and isinstance(value, list):
        parts = [_split(row, section=name) for row in value]
        return [part[0] for part in parts], {str(i): part[1] for i, part in enumerate(parts) if part[1]}
    body, observations = _split(value, section=name)
    return body, {"": observations} if observations else {}


def _sections(snapshot: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result = {}
    for name, value in snapshot.items():
        if not isinstance(name, str):
            raise ValueError("Heartbeat section names must be strings")
        body, groups = _section(name, value)
        # Explicit layout makes absent/null and row/layout changes unambiguous.
        layout = {group: sorted(observations) for group, observations in groups.items()}
        result[name] = {"b": digest([body, layout], short=True),
                        "o": {group: digest(observations, short=True) for group, observations in groups.items()}}
    return result


def knowledge(snapshot: Mapping[str, Any], scope: str) -> dict[str, Any]:
    """Knowledge is sent only alongside its intact, locally retained snapshot."""
    return {"scope": scope, "revision": digest(snapshot), "sections": _sections(snapshot)}


def _valid_known(value: Any, scope: str) -> bool:
    if not isinstance(value, dict) or set(value) != {"scope", "revision", "sections"}:
        return False
    if value["scope"] != scope or not isinstance(value["revision"], str) or not _FULL.fullmatch(value["revision"]):
        return False
    sections = value["sections"]
    if not isinstance(sections, dict):
        return False
    for name, entry in sections.items():
        if not isinstance(name, str) or not isinstance(entry, dict) or set(entry) != {"b", "o"}:
            return False
        if not isinstance(entry["b"], str) or not _SHORT.fullmatch(entry["b"]) or not isinstance(entry["o"], dict):
            return False
        if any(not isinstance(group, str) or not isinstance(token, str) or not _SHORT.fullmatch(token)
               for group, token in entry["o"].items()):
            return False
    return len(canonical_bytes(value)) <= MAX_KNOWN_BYTES


_REQUIRED_SCOPE = frozenset({"tenant", "platform_project", "bundle", "viewer", "authority", "view"})


def reply_scope_id(scope: Mapping[str, str]) -> str:
    """The scope digest a reply carries, or "" when the scope is incomplete."""
    if not _REQUIRED_SCOPE.issubset(scope) or any(not scope[key] for key in _REQUIRED_SCOPE):
        return ""
    return digest({**scope, "protocol": REPLY_PROTOCOL})


def valid_known_digest(value: Any) -> bool:
    return isinstance(value, str) and _FULL.fullmatch(value) is not None


def known_record(snapshot: Mapping[str, Any], scope_id: str) -> tuple[str, bytes] | None:
    """The digest and canonical bytes of the knowledge a relay holds after this reply.

    A relay that reconstructed ``snapshot`` computes exactly this knowledge, so
    the Board can keep it under its digest and the relay can echo only the
    digest (W827 S2b). None when the knowledge would not be sent at all.
    """
    try:
        value = knowledge(snapshot, scope_id)
        raw = canonical_bytes(value)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None
    if len(raw) > MAX_KNOWN_BYTES:
        return None
    return hashlib.sha256(raw).hexdigest(), raw


def known_from_record(raw: Any, expected_digest: str, scope_id: str) -> dict[str, Any] | None:
    """Knowledge kept under ``expected_digest``, verified, or None (a full reply follows)."""
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw)
    elif isinstance(raw, str):
        raw = raw.encode("utf-8")
    else:
        return None
    if not valid_known_digest(expected_digest) or hashlib.sha256(raw).hexdigest() != expected_digest:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if _valid_known(value, scope_id) else None


def reply_marker(response: Any) -> Mapping[str, Any]:
    marker = response.get("heartbeat_reply") if isinstance(response, Mapping) else None
    return marker if isinstance(marker, Mapping) and marker.get("protocol") == REPLY_PROTOCOL else {}


def reply_features(response: Any) -> frozenset[str]:
    """The request features this reply's Board accepts; empty for an old Board."""
    features = reply_marker(response).get("features")
    if not isinstance(features, list):
        return frozenset()
    return frozenset(name for name in features if isinstance(name, str) and name in REQUEST_FEATURES)


def encode_reply(snapshot: dict[str, Any], *, protocol: Any, scope: Mapping[str, str],
                 known: Any = None) -> dict[str, Any]:
    """Select fresh wire data without retaining a server baseline or cursor."""
    if protocol != REPLY_PROTOCOL or not reply_scope_id(scope):
        return snapshot
    try:
        scope_id = reply_scope_id(scope)
        revision = digest(snapshot)
        marker = {"protocol": REPLY_PROTOCOL, "scope": scope_id, "revision": revision}
        if not _valid_known(known, scope_id):
            return {"heartbeat_reply": {**marker, "state": "full", "snapshot": snapshot}}
        current = _sections(snapshot)
        replacements, updates = {}, {}
        for name, entry in current.items():
            previous = known["sections"].get(name)
            if previous is None or previous["b"] != entry["b"]:
                replacements[name] = snapshot[name]
                continue
            # A matching body binds the exact group layout. A malformed echo
            # is a full recovery, not a partial/truncated view.
            if set(previous["o"]) != set(entry["o"]):
                return {"heartbeat_reply": {**marker, "state": "full", "snapshot": snapshot}}
            _, groups = _section(name, snapshot[name])
            changed = {group: groups[group] for group, token in entry["o"].items()
                       if previous["o"][group] != token}
            if changed:
                updates[name] = changed
        delta = {**marker, "state": "delta", "base": known["revision"]}
        if replacements:
            delta["set"] = replacements
        if updates:
            delta["observations"] = updates
        removed = sorted(set(known["sections"]) - set(current))
        if removed:
            delta["remove"] = removed
        return {"heartbeat_reply": delta}
    except (TypeError, ValueError, OverflowError, RecursionError):
        # An unsupported display stays readable by the existing legacy path.
        return snapshot


def _restore(body: Any, observations: Mapping[str, Any]) -> Any:
    if "" in observations:
        return deepcopy(observations[""])
    result = deepcopy(body)
    for pointer, value in observations.items():
        parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]
        node = result
        for part in parts[:-1]:
            node = node[int(part)] if isinstance(node, list) else node[part]
        if isinstance(node, list):
            node[int(parts[-1])] = deepcopy(value)
        else:
            node[parts[-1]] = deepcopy(value)
    return result


def decode_reply(response: Mapping[str, Any], *, baseline: Mapping[str, Any] | None = None,
                 scope: str = "") -> tuple[dict[str, Any], str]:
    """Reconstruct and verify before materializing; malformed deltas must retry full."""
    if "heartbeat_reply" not in response:
        return deepcopy(dict(response)), ""
    marker = response["heartbeat_reply"]
    if not isinstance(marker, dict) or marker.get("protocol") != REPLY_PROTOCOL:
        raise ValueError("Unsupported heartbeat reply")
    scope_id, revision = marker.get("scope"), marker.get("revision")
    if not isinstance(scope_id, str) or not _FULL.fullmatch(scope_id) or not isinstance(revision, str) or not _FULL.fullmatch(revision):
        raise ValueError("Invalid heartbeat reply identity")
    if marker.get("state") == "full":
        snapshot = marker.get("snapshot")
        if not isinstance(snapshot, dict):
            raise ValueError("Missing full heartbeat snapshot")
        snapshot = deepcopy(snapshot)
    elif marker.get("state") == "delta":
        if baseline is None or scope != scope_id or marker.get("base") != digest(baseline):
            raise ValueError("Heartbeat baseline unavailable")
        snapshot = deepcopy(dict(baseline))
        replacements, updates, removed = marker.get("set", {}), marker.get("observations", {}), marker.get("remove", [])
        if not isinstance(replacements, dict) or not isinstance(updates, dict) or not isinstance(removed, list):
            raise ValueError("Invalid heartbeat delta shape")
        if any(not isinstance(name, str) for name in [*replacements, *updates, *removed]):
            raise ValueError("Invalid heartbeat section name")
        if set(replacements) & set(updates) or (set(replacements) | set(updates)) & set(removed) or len(set(removed)) != len(removed):
            raise ValueError("Conflicting heartbeat section edits")
        for name in removed:
            if name not in snapshot:
                raise ValueError("Unknown removed heartbeat section")
            del snapshot[name]
        snapshot.update(deepcopy(replacements))
        for name, changed in updates.items():
            if name not in snapshot or not isinstance(changed, dict):
                raise ValueError("Unknown heartbeat observation section")
            body, groups = _section(name, snapshot[name])
            for group, observations in changed.items():
                if group not in groups or not isinstance(observations, dict) or set(observations) != set(groups[group]):
                    raise ValueError("Heartbeat observation layout changed")
                if any(isinstance(value, (dict, list)) for value in observations.values()):
                    raise ValueError("Heartbeat observation is not a leaf")
                groups[group] = observations
            if name == "team" and isinstance(body, list):
                snapshot[name] = [_restore(row, groups.get(str(i), {})) for i, row in enumerate(body)]
            else:
                snapshot[name] = _restore(body, groups.get("", {}))
    else:
        raise ValueError("Unknown heartbeat reply state")
    if digest(snapshot) != revision:
        raise ValueError("Heartbeat reconstruction digest mismatch")
    return snapshot, scope_id
