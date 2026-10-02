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
  no-proxy-record          no proxy line with this id: the hop is unknown
  no-request-id            the failure predates request ids
  lock-holder-seen:<spans> a lock span held 0.25 s or more within 15 s
  no-holder-seen           spans were logged in the window, none held long
  no-span-evidence         no span lines in the window (before W461, or
                           span logging below INFO)
  loop-blocked:<n>         n relay loop-block or stall lines within 5 s

Usage:
  scripts/oauth_failure_triage.py --relay-log <relay.stderr.log> \\
      --since 2026-10-02T10:00:00Z --until 2026-10-02T10:30:00Z \\
      [--proxy-container custom-ui-managed-infra-web-proxy-1] \\
      [--tunnel-log ~/Library/Logs/ngrok/ngrok.log] [--worker <name fragment>]

It prints one line per failure and a count per verdict. It never prints a
token, a header, a body or a URL host.
"""

from __future__ import annotations

import argparse
import collections
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

NEAR = timedelta(seconds=15)
TUNNEL_NEAR = timedelta(seconds=5)
LOOP_NEAR = timedelta(seconds=5)
SLOW_MS = 250

_FAILED = re.compile(r"worker channel failed worker=(?P<worker>\S+).*?error_code=(?P<code>oauth_\w+)")
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
                failures.append({
                    "at": at,
                    "worker": failed.group("worker"),
                    "code": failed.group("code"),
                    "kind": kind.group("kind") if kind else "",
                    "rid": rid.group("rid") if rid else "",
                })
    return failures, spans, loops


def proxy_rids(container: str, since: datetime, until: datetime) -> dict[str, str]:
    if not container:
        return {}
    result = subprocess.run(
        ["docker", "logs", "--since", (since - NEAR).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "--until", (until + NEAR).strftime("%Y-%m-%dT%H:%M:%SZ"), container],
        capture_output=True, text=True, check=False,
    )
    found: dict[str, str] = {}
    for line in (result.stdout + result.stderr).splitlines():
        rid = _PROXY_RID.search(line)
        if rid:
            status = _PROXY_STATUS.search(line)
            found[rid.group("rid")] = status.group("status") if status else "?"
    return found


def tunnel_lines(path: Path | None, at: datetime) -> list[str]:
    if path is None or not path.exists():
        return []
    near = []
    with path.open(errors="replace") as handle:
        for line in handle:
            stamp = re.search(r"t=(\S+)", line)
            if not stamp:
                continue
            try:
                when = datetime.fromisoformat(stamp.group(1).replace("Z", "+00:00")).astimezone(timezone.utc)
            except ValueError:
                continue
            if abs(when - at) <= TUNNEL_NEAR and re.search(r"lvl=(warn|eror|crit)", line):
                # Keep only level and message: no addresses, hosts or ids.
                msg = re.search(r'msg="?([^"]*)"?', line)
                near.append((msg.group(1) if msg else "?")[:80])
    return near


def classify(failure, spans, loops, rids, tunnel_log):
    labels = []
    if failure["code"] == "oauth_profile_lock_timeout":
        holders = [
            s for at, s in spans
            if abs(at - failure["at"]) <= NEAR and s["hold"] not in ("-",) and int(s["hold"]) >= SLOW_MS
            and s["kind"] in ("transaction", "refresh_slot")
        ]
        if holders:
            labels.append("lock-holder-seen:" + ",".join(
                sorted({f"{h['kind']}/{h['op']}/{h['hold']}ms" for h in holders})))
        else:
            labels.append("no-holder-seen" if spans else "no-span-evidence")
    elif failure["rid"]:
        status = rids.get(failure["rid"])
        labels.append(f"reached-proxy:{status}" if status else "no-proxy-record")
    else:
        labels.append("no-request-id")
    blocked = sum(1 for at in loops if abs(at - failure["at"]) <= LOOP_NEAR)
    if blocked:
        labels.append(f"loop-blocked:{blocked}")
    tunnel = tunnel_lines(tunnel_log, failure["at"])
    if tunnel:
        labels.append("tunnel:" + "|".join(sorted(set(tunnel))[:3]))
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
          f"{len(loops)} loop-block lines, {len(rids)} proxy request ids")
    for label, count in sorted(counts.items()):
        print(f"{label}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
