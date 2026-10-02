#!/usr/bin/env python3
"""Classify a relay's OAuth failures in a time window, from the evidence that exists (W461).

Read-only. For each OAuth failure of a host relay in the window it looks at
the three sides that can each see part of a request, and names what the
evidence shows instead of guessing a cause:

- the relay log (``relay.stderr.log``): the failure, its code, failure kind
  and the request id the client sent (Connection Hub W461 and later);
- the web proxy access log (``docker logs`` of the proxy container): a line
  carrying ``rid="<request id>"`` and its status, or no such line;
- the tunnel agent log, when it is configured: lines near the failure time;
- for ``oauth_profile_lock_timeout``: the lock spans of the same window,
  that is whether any span held a lock for a long time (a real holder) or
  none did (the waiter was starved or queued), and relay loop-block lines.

Verdicts are evidence labels, not proven causes:

  reached-proxy:<status>   the proxy logged the request id with this status
  no-proxy-record          the proxy log was read and has no line with this id
  proxy-log-unavailable    the proxy log could not be read: no evidence either way
  no-request-id            the failure predates request ids
  holder-overlap:<spans>   a span of the same lock kind and profile tag was
                           held during this failure's wait (an overlap, which
                           is a candidate holder, not proof of the cause)
  no-overlapping-holder    spans were logged, none of the same lock overlapped
                           the wait (absence does not prove starvation)
  no-span-evidence         no span lines in the window (before W461, or
                           span logging below INFO)
  loop-blocked:<n>         n relay loop-block or stall lines within 5 s
  tunnel-warn:<n>          n tunnel lines at warn level or above within 5 s
                           (counts only, no message text)

Usage:
  scripts/oauth_failure_triage.py --relay-log <relay.stderr.log> \\
      --since 2026-10-02T10:00:00Z --until 2026-10-02T10:30:00Z \\
      [--proxy-container custom-ui-managed-infra-web-proxy-1] \\
      [--tunnel-log ~/Library/Logs/ngrok/ngrok.log] [--worker <name fragment>]

It prints one line per failure and a count per verdict. It never prints a
token, a header, a body, a URL host or any tunnel message text.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

NEAR = timedelta(seconds=15)
TUNNEL_NEAR = timedelta(seconds=5)
LOOP_NEAR = timedelta(seconds=5)
LOCK_TIMEOUT = timedelta(seconds=10)
SLOW_MS = 250

_FAILED = re.compile(r"worker channel failed worker=(?P<worker>\S+).*?error_code=(?P<code>oauth_\w+)")
_PROFILE = re.compile(r"\bprofile=(?P<profile>\S+)")
_KIND = re.compile(r'failure_kind="?(?P<kind>\w+)')
_RID = re.compile(r"request_id[ =](?P<rid>[0-9a-f]{16})")
_SPAN = re.compile(
    r"Connection Hub OAuth span kind=(?P<kind>\w+) operation=(?P<op>\S+) profile=(?P<profile>\w+) "
    r"outcome=(?P<outcome>\S+) wait_ms=(?P<wait>[-\d]+) hold_ms=(?P<hold>[-\d]+) pid=(?P<pid>\d+) task=(?P<task>\S+)"
)
_LOOP = re.compile(r"relay loop (blocked|stalled)")
_PROXY_RID = re.compile(r'rid="(?P<rid>[0-9a-f]{16})"')
_PROXY_STATUS = re.compile(r'" (?P<status>\d{3}) ')


def _when(text: str) -> datetime | None:
    try:
        return datetime.strptime(text[:23], "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_z(text: str) -> datetime:
    return datetime.strptime(text.rstrip("Z"), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def relay_events(path: Path, since: datetime, until: datetime, worker: str):
    failures, spans, loops = [], [], []
    with path.open(errors="replace") as handle:
        for line in handle:
            at = _when(line)
            if at is None or not (since - NEAR <= at <= until + NEAR):
                continue
            if _LOOP.search(line):
                loops.append(at)
                continue
            span = _SPAN.search(line)
            if span:
                spans.append((at, span.groupdict()))
                continue
            failed = _FAILED.search(line)
            if failed and since <= at <= until and worker in failed.group("worker"):
                kind = _KIND.search(line)
                rid = _RID.search(line)
                profile = _PROFILE.search(line)
                failures.append({
                    "at": at,
                    "worker": failed.group("worker"),
                    "code": failed.group("code"),
                    "kind": kind.group("kind") if kind else "",
                    "rid": rid.group("rid") if rid else "",
                    # Spans carry this tag, never the clear name.
                    "profile_tag": hashlib.sha256(profile.group("profile").encode()).hexdigest()[:12]
                    if profile else "",
                })
    return failures, spans, loops


def proxy_rids(container: str, since: datetime, until: datetime) -> dict[str, str] | None:
    """Request id -> status from the proxy log, or None when it could not be read."""

    if not container:
        return None
    try:
        result = subprocess.run(
            ["docker", "logs", "--since", (since - NEAR).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "--until", (until + NEAR).strftime("%Y-%m-%dT%H:%M:%SZ"), container],
            capture_output=True, text=True, check=False, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    found: dict[str, str] = {}
    for line in (result.stdout + result.stderr).splitlines():
        rid = _PROXY_RID.search(line)
        if rid:
            status = _PROXY_STATUS.search(line)
            found[rid.group("rid")] = status.group("status") if status else "?"
    return found


def tunnel_warnings(path: Path | None, at: datetime) -> int:
    """How many tunnel lines at warn level or above fall within TUNNEL_NEAR.

    Only a count: tunnel messages can carry hosts, addresses or tokens.
    """

    if path is None or not path.exists():
        return 0
    count = 0
    with path.open(errors="replace") as handle:
        for line in handle:
            if not re.search(r"\blvl=(warn|eror|crit)\b", line):
                continue
            stamp = re.search(r"\bt=(\S+)", line)
            if not stamp:
                continue
            try:
                when = datetime.fromisoformat(stamp.group(1).replace("Z", "+00:00")).astimezone(timezone.utc)
            except ValueError:
                continue
            if abs(when - at) <= TUNNEL_NEAR:
                count += 1
    return count


_LOCK_KIND = {"oauth_profile_lock_timeout": ("transaction", "refresh_slot")}


def classify(failure, spans, loops, rids, tunnel_log):
    labels = []
    if failure["code"] == "oauth_profile_lock_timeout":
        # The failed wait ran from (failure - LOCK_TIMEOUT) to the failure.
        wait_start = failure["at"] - LOCK_TIMEOUT
        overlapping = []
        for at, s in spans:
            if s["kind"] not in _LOCK_KIND[failure["code"]] or s["hold"] == "-":
                continue
            # The refresh slot is per profile, so only the same profile's slot can block it.
            if s["kind"] == "refresh_slot" and s["profile"] != failure.get("profile_tag", ""):
                continue
            held_from = at - timedelta(milliseconds=int(s["hold"]))
            if held_from < failure["at"] and at > wait_start:
                overlapping.append(s)
        if overlapping:
            labels.append("holder-overlap:" + ",".join(
                sorted({f"{h['kind']}/{h['op']}/{h['profile']}/{h['outcome']}/{h['hold']}ms" for h in overlapping})))
        else:
            labels.append("no-overlapping-holder" if spans else "no-span-evidence")
    elif not failure["rid"]:
        labels.append("no-request-id")
    elif rids is None:
        labels.append("proxy-log-unavailable")
    else:
        status = rids.get(failure["rid"])
        labels.append(f"reached-proxy:{status}" if status else "no-proxy-record")
    blocked = sum(1 for at in loops if abs(at - failure["at"]) <= LOOP_NEAR)
    if blocked:
        labels.append(f"loop-blocked:{blocked}")
    warned = tunnel_warnings(tunnel_log, failure["at"])
    if warned:
        labels.append(f"tunnel-warn:{warned}")
    return labels


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--relay-log", required=True, type=Path)
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True)
    parser.add_argument("--proxy-container", default="")
    parser.add_argument("--tunnel-log", type=Path, default=None)
    parser.add_argument("--worker", default="")
    args = parser.parse_args()
    since, until = _parse_z(args.since), _parse_z(args.until)
    failures, spans, loops = relay_events(args.relay_log, since, until, args.worker)
    rids = proxy_rids(args.proxy_container, since, until)
    counts: collections.Counter[str] = collections.Counter()
    for failure in failures:
        labels = classify(failure, spans, loops, rids, args.tunnel_log)
        counts.update(label.split(":")[0] for label in labels)
        print(
            failure["at"].strftime("%H:%M:%S.%f")[:-3] + "Z",
            failure["worker"][-12:], failure["code"], failure["kind"] or "-",
            failure["rid"] or "-", " ".join(labels),
        )
    long_holds = [s for _, s in spans if s["hold"] != "-" and int(s["hold"]) >= SLOW_MS]
    print(f"--- {len(failures)} OAuth failures, {len(spans)} spans ({len(long_holds)} held 250 ms or more), "
          f"{len(loops)} loop-block lines, "
          + ("proxy log unavailable" if rids is None else f"{len(rids)} proxy request ids"))
    for label, count in sorted(counts.items()):
        print(f"{label}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
