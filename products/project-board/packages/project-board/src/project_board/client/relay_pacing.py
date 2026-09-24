"""How often the host relay may call the gateway, per channel and per host.

Why: on 2026-09-21 three dead worker channels were retried every 30-second
cycle, each retry running a protected-resource GET, an authorization-server
GET and a token POST. That exhausted the gateway's anonymous hourly bucket
(3,033 of 1,200), every 429 carried ``retry_after: 3600``, the relay ignored
it, and the operator's own ``pb worker authorize`` shared the exhausted
bucket, so the one action that could repair the channels was refused.

Four rules, all enforced here:

    host        a 429 (or a 503 naming Retry-After) quiets every call to the
                gateway from this relay until the named time passes
    channel     a transient failure backs a channel off, doubling from one
                minute to about thirty with jitter, and success resets it.
                One exception: when the server accepted the connection and
                only the Data Bus namespace handshake timed out, the channel
                retries within seconds, capped at one minute, for a bounded
                number of attempts before the normal backoff applies. Why: a
                channel can be accepted repeatedly while each namespace
                handshake times out under load after relay restarts, and
                the doubling pushed the next attempt more than 20 minutes out
                while the worker could not reach the board.
    runtime     a failure that says the runtime is not there (the MCP
                endpoint answering 404, a connection refused, a 502 or 504)
                is a state of the world, not a refusal. The channel retries
                after 5 s, then every 10 s for fifteen minutes from the
                streak's first failure, then once a minute, never the
                doubling, and those attempts do not count toward it. Why: on
                2026-09-23 five such failures across an eleven-minute rebuild
                grew the backoff to ten minutes past recovery, and a relay
                restart kept it, since the schedule is on disk. A relay start
                forgets these records, and one channel opening after such a
                failure clears every other channel's record, because the
                runtime that answered one answers all.
    pending     a channel waiting for authorization is retried at once when
                its local profile changes (what ``pb worker authorize`` does),
                otherwise after its backoff; a permanent refusal waits the
                capped thirty minutes, because an operator may fix it on the
                server (a grant, a deploy) without touching the profile

State is kept next to the host config, so a relay restart inside a rate-limit
window does not start hammering again. A relay start decides once per channel
and logs the decision (``relay pacing restart ... decision=``):

    attempted         a channel backing off from a transient refusal, waiting
                      on the runtime, or refused permanently for a reason a
                      server-side fix can clear, is tried at once. A relay is
                      usually restarted because the server was fixed: on
                      2026-09-24 two channels refused while Card admission was
                      broken reconnected fifteen minutes after the fix,
                      because their doubled backoff survived the restart. If
                      that attempt fails, the channel returns to its schedule
                      from its accumulated count, never faster than before.
    kept_backoff      the host is inside a gateway rate-limit window, which a
                      restart does not lift, so every channel keeps its time,
                      or a channel backing off from a credential refusal keeps
                      its schedule, since the credential's answer is the same
    parked_permanent  a credential the server refused (a revoked refresh
                      family, a revoked or unknown Card) stays parked until its
                      profile fingerprint changes, which is what
                      ``pb worker authorize`` does. A restart never retries a
                      credential known to be dead.

To clear everything, including the host quiet window, stop the relay and delete
``relay-pacing.json`` beside the host config. The state holds no secret: times,
counts, reason codes, and a fingerprint of non-secret profile fields.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .relay_admission import RUNTIME_UNAVAILABLE_CODES

logger = logging.getLogger(__name__)

PACING_FILENAME = "relay-pacing.json"
CHANNEL_BACKOFF_BASE_SECONDS = 60.0
CHANNEL_BACKOFF_CAP_SECONDS = 1800.0
# An accepted connection whose namespace handshake timed out.
HANDSHAKE_RETRY_BASE_SECONDS = 5.0
HANDSHAKE_RETRY_CAP_SECONDS = 60.0
HANDSHAKE_RETRY_LIMIT = 6
HANDSHAKE_TIMEOUT_REASON = "data_bus_namespace_timeout"
# The runtime is not there: retried soon, for as long as an outage plausibly
# lasts, then once a minute so a runtime that is misdeployed for good costs
# sixty probes an hour per channel and never the gateway's bucket.
RUNTIME_RETRY_BASE_SECONDS = 5.0
RUNTIME_RETRY_CAP_SECONDS = 10.0
RUNTIME_RETRY_WINDOW_SECONDS = 15 * 60.0
RUNTIME_RETRY_LONG_SECONDS = 60.0
RUNTIME_SCHEDULE = "runtime"
# The server refused the credential itself: only a new authorization, which
# changes the profile fingerprint, can change that answer.
CREDENTIAL_REFUSAL_REASONS = frozenset(
    {
        "oauth_token_request_failed",
        "delegated_card_refresh_refused",
        "delegated_card_revoked",
        "delegated_card_not_found",
    }
)
RESTART_ATTEMPTED = "attempted"
RESTART_KEPT_BACKOFF = "kept_backoff"
RESTART_PARKED_PERMANENT = "parked_permanent"
# A 429 that names no wait still means the bucket is spent.
HOST_QUIET_DEFAULT_SECONDS = 60.0
# The gateway's own window is an hour. A longer Retry-After (a wrong value, or
# clock skew on an HTTP date) would silence every worker on the host.
HOST_QUIET_CAP_SECONDS = 3600.0


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _error_chain(error: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in chain and len(chain) < 8:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def credential_refusal(reason: str) -> bool:
    """Whether a refusal reason says the credential itself is dead."""

    text = str(reason or "")
    return any(code in text for code in CREDENTIAL_REFUSAL_REASONS)


def rate_limit_wait(error: BaseException) -> float | None:
    """Seconds the gateway asked this host to wait, or ``None`` if not rate limited.

    Reads the status and ``retry_after_seconds`` a failure carries, on the
    error or anything it wraps. A 429 without a wait uses the default window.
    A 503 counts only when it names a wait.
    """

    for item in _error_chain(error):
        details = getattr(item, "details", None)
        details = details if isinstance(details, Mapping) else {}
        try:
            status = int(details.get("status") or getattr(item, "status", 0) or 0)
        except (TypeError, ValueError):
            status = 0
        wait = details.get("retry_after_seconds")
        if isinstance(wait, (int, float)) and not isinstance(wait, bool) and status in {429, 503}:
            return float(max(0.0, min(HOST_QUIET_CAP_SECONDS, wait)))
        if status == 429:
            return HOST_QUIET_DEFAULT_SECONDS
    return None


class RelayPacing:
    def __init__(
        self,
        path: Path | None,
        *,
        clock: Callable[[], float] = time.time,
        rng: Callable[[], float] = random.random,
        forget_permanent: bool = False,
    ) -> None:
        self._path = path
        self._clock = clock
        self._rng = rng
        self._state: dict[str, Any] = {"host_quiet_until": 0.0, "channels": {}, "pending": {}}
        self.restart_decisions: dict[str, dict[str, str]] = {}
        self._load()
        if forget_permanent:
            self._forget_on_start()

    # -- persistence ----------------------------------------------------------

    def _load(self) -> None:
        if self._path is None or not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("Relay pacing state is unreadable; starting clean path=%s", self._path)
            return
        if isinstance(data, Mapping):
            self._state["host_quiet_until"] = float(data.get("host_quiet_until") or 0.0)
            for key in ("channels", "pending"):
                value = data.get(key)
                if isinstance(value, Mapping):
                    self._state[key] = {str(k): dict(v) for k, v in value.items() if isinstance(v, Mapping)}

    def _forget_on_start(self) -> None:
        """Decide, once per channel, what a relay start does with its record.

        See the module docstring for the three decisions and why. Each one is
        logged, and kept in ``restart_decisions`` for the relay's evidence.
        """

        now = self._clock()
        quiet = float(self._state["host_quiet_until"]) > now
        decisions: dict[str, tuple[str, str]] = {}
        changed = False
        for name, record in list(self._state["pending"].items()):
            if not record.get("permanent"):
                continue
            reason = str(record.get("reason") or "")
            if credential_refusal(reason):
                decisions[name] = (RESTART_PARKED_PERMANENT, reason)
                continue
            # The fix may have been made on the server while the relay was down.
            self._state["pending"].pop(name, None)
            self._state["channels"].pop(name, None)
            decisions[name] = (RESTART_ATTEMPTED, reason)
            changed = True
        # Records of the runtime being down, on either schedule: the runtime
        # schedule, or a doubling backoff whose reason is a runtime code, as a
        # relay from before the runtime schedule wrote them during an outage.
        # On 2026-09-23 such a record kept one Codex channel closed for five
        # minutes after the rebuilt relay started, and its mail was not even
        # pulled, so no wake could follow.
        runtime = self._runtime_channels() + [
            name
            for name, record in self._state["channels"].items()
            if record.get("schedule") != RUNTIME_SCHEDULE
            and str(record.get("reason") or "") in RUNTIME_UNAVAILABLE_CODES
        ]
        for name in runtime:
            record = self._state["channels"].pop(name, None) or {}
            decisions[name] = (RESTART_ATTEMPTED, str(record.get("reason") or ""))
            changed = True
        for name, record in self._state["channels"].items():
            if name in decisions:
                continue
            reason = str(record.get("reason") or "")
            if quiet:
                decisions[name] = (RESTART_KEPT_BACKOFF, "host_rate_limited")
                continue
            if credential_refusal(reason):
                # The credential's own answer: a restart does not change it.
                decisions[name] = (RESTART_KEPT_BACKOFF, reason)
                continue
            # One attempt now. The accumulated count stays, so a failure
            # returns to the schedule it had, never a faster one.
            if float(record.get("next_at") or 0.0) > now:
                record["next_at"] = now
                changed = True
            decisions[name] = (RESTART_ATTEMPTED, reason)
        if any(decision == RESTART_ATTEMPTED for decision, _ in decisions.values()):
            self._state["host_quiet_until"] = min(
                float(self._state["host_quiet_until"]),
                now + HOST_QUIET_CAP_SECONDS,
            )
        self.restart_decisions = {
            name: {"decision": decision, "reason": reason}
            for name, (decision, reason) in sorted(decisions.items())
        }
        for name, (decision, reason) in sorted(decisions.items()):
            logger.info(
                "relay pacing restart worker=%s decision=%s reason=%s",
                name,
                decision,
                reason or "-",
            )
        if changed:
            self._save()

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self._state, sort_keys=True), encoding="utf-8")
            os.replace(temporary, self._path)
        except OSError:
            logger.warning("Could not persist relay pacing state path=%s", self._path, exc_info=True)

    # -- host -----------------------------------------------------------------

    def host_quiet_seconds(self) -> float:
        return max(0.0, float(self._state["host_quiet_until"]) - self._clock())

    def observe(self, error: BaseException) -> bool:
        """Quiet the host if ``error`` is a rate limit. True when it was."""

        wait = rate_limit_wait(error)
        if wait is None:
            return False
        until = self._clock() + wait
        if until > float(self._state["host_quiet_until"]):
            self._state["host_quiet_until"] = until
            self._save()
            logger.warning(
                "Gateway rate limited this relay; no gateway calls until %s (%.0f s)",
                _iso(until),
                wait,
            )
        return True

    # -- channels -------------------------------------------------------------

    def channel_due(self, name: str) -> bool:
        record = self._state["channels"].get(name)
        return record is None or float(record.get("next_at") or 0.0) <= self._clock()

    def record_failure(
        self,
        name: str,
        reason: str,
        *,
        handshake_timeout: bool = False,
        runtime_unavailable: bool = False,
    ) -> float:
        """Back ``name`` off after a transient failure; return the delay.

        ``handshake_timeout`` marks an accepted connection whose namespace
        handshake timed out. It retries on the short schedule until
        ``HANDSHAKE_RETRY_LIMIT`` such attempts, then on the normal one.
        ``runtime_unavailable`` marks a runtime that is not there. It retries
        on the runtime schedule for as long as the streak lasts, and its
        attempts never feed the doubling.
        """

        now = self._clock()
        record = self._state["channels"].setdefault(name, {"attempts": 0})
        attempts = int(record.get("attempts") or 0) + 1
        quick = int(record.get("handshake_attempts") or 0)
        runtime_attempts = int(record.get("runtime_attempts") or 0)
        runtime_since = record.get("runtime_since")
        if runtime_unavailable:
            runtime_attempts += 1
            if not isinstance(runtime_since, (int, float)) or record.get("schedule") != RUNTIME_SCHEDULE:
                runtime_since = now
            if now - float(runtime_since) < RUNTIME_RETRY_WINDOW_SECONDS:
                delay = min(
                    RUNTIME_RETRY_CAP_SECONDS,
                    RUNTIME_RETRY_BASE_SECONDS * (2 ** (runtime_attempts - 1)),
                )
            else:
                delay = RUNTIME_RETRY_LONG_SECONDS
            schedule = RUNTIME_SCHEDULE
        elif handshake_timeout and quick < HANDSHAKE_RETRY_LIMIT:
            quick += 1
            delay = min(
                HANDSHAKE_RETRY_CAP_SECONDS,
                HANDSHAKE_RETRY_BASE_SECONDS * (2 ** (quick - 1)),
            )
            schedule = "handshake"
            runtime_since = None
        else:
            # Only normal attempts count toward the doubling, so a channel
            # leaving the short handshake or runtime retries starts again at
            # one minute instead of jumping to the cap.
            normal = max(1, attempts - quick - runtime_attempts)
            delay = min(
                CHANNEL_BACKOFF_CAP_SECONDS,
                CHANNEL_BACKOFF_BASE_SECONDS * (2 ** (normal - 1)),
            )
            schedule = "backoff"
            runtime_since = None
        if schedule != RUNTIME_SCHEDULE:
            delay *= 0.5 + 0.5 * float(self._rng())
        record.update(
            attempts=attempts,
            handshake_attempts=quick,
            runtime_attempts=runtime_attempts,
            next_at=now + delay,
            reason=str(reason),
            schedule=schedule,
            failed_at=now,
        )
        if runtime_since is None:
            record.pop("runtime_since", None)
        else:
            record["runtime_since"] = float(runtime_since)
        self._save()
        return delay

    def record_success(self, name: str) -> None:
        """Forget ``name``'s failures. A channel that was waiting on the
        runtime clears every other channel waiting on it too: the runtime that
        answered this one answers them."""

        record = self._state["channels"].pop(name, None)
        changed = record is not None
        changed = (self._state["pending"].pop(name, None) is not None) or changed
        if record is not None and record.get("schedule") == RUNTIME_SCHEDULE:
            for other in self._runtime_channels():
                self._state["channels"].pop(other, None)
                changed = True
        if changed:
            self._save()

    def _runtime_channels(self) -> list[str]:
        return [
            name
            for name, record in self._state["channels"].items()
            if record.get("schedule") == RUNTIME_SCHEDULE
        ]

    def soonest_runtime_retry_seconds(self) -> float | None:
        """Seconds until the next retry of a channel waiting on the runtime,
        or None when no channel is. The cycle shortens itself to this."""

        now = self._clock()
        waits = [
            max(0.0, float(self._state["channels"][name].get("next_at") or 0.0) - now)
            for name in self._runtime_channels()
        ]
        return min(waits) if waits else None

    # -- pending authorization -------------------------------------------------

    def pending_due(self, name: str, fingerprint: str) -> bool:
        """Whether a pending channel may call the gateway this cycle."""

        record = self._state["pending"].get(name)
        if record is None:
            return True
        if str(record.get("fingerprint") or "") != str(fingerprint):
            # pb worker authorize rewrote the profile: retry now, no backoff.
            self._state["channels"].pop(name, None)
            return True
        return self.channel_due(name)

    def record_pending_refusal(
        self, name: str, *, fingerprint: str, permanent: bool, reason: str
    ) -> None:
        self._state["pending"][name] = {
            "fingerprint": str(fingerprint),
            "permanent": bool(permanent),
            "reason": str(reason),
            "refused_at": self._clock(),
        }
        if permanent:
            # Retried at the capped interval, since the fix may be server-side.
            self._state["channels"][name] = {
                "attempts": int((self._state["channels"].get(name) or {}).get("attempts") or 0),
                "next_at": self._clock() + CHANNEL_BACKOFF_CAP_SECONDS,
                "reason": str(reason),
            }
        self._save()

    # -- diagnostics ----------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """What an operator needs to read why a channel is quiet."""

        now = self._clock()
        quiet_until = float(self._state["host_quiet_until"])
        channels = {
            name: _channel_view(record, now) for name, record in self._state["channels"].items()
        }
        pending = {
            name: {
                "reason": str(record.get("reason") or ""),
                "retry": (
                    "after pb worker authorize: the server refused this credential"
                    if record.get("permanent") and credential_refusal(str(record.get("reason") or ""))
                    else "after pb worker authorize, a relay restart, or in 30 minutes"
                    if record.get("permanent")
                    else "after pb worker authorize, or at the channel's next attempt"
                ),
            }
            for name, record in self._state["pending"].items()
        }
        return {
            "host_quiet_until": _iso(quiet_until) if quiet_until > now else "",
            "channels": channels,
            "pending": pending,
        }


def _channel_view(record: Mapping[str, Any], now: float) -> dict[str, Any]:
    return {
        "attempts": int(record.get("attempts") or 0),
        "reason": str(record.get("reason") or ""),
        "schedule": str(record.get("schedule") or "backoff"),
        "next_attempt_at": _iso(float(record.get("next_at") or now)),
    }


def channel_reconnect_state(
    config_path: str | Path,
    worker_name: str,
    *,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any] | None:
    """Read, without writing, whether the relay is reconnecting ``worker_name``.

    The relay keeps a channel record only while that channel is failing, and
    removes it on the first success. A record therefore means the channel is
    not open now: the answer carries the last error, the attempt count and the
    next attempt time. ``None`` means the relay has no failure on record.
    A channel waiting for authorization is reported by its channel state, not
    here.
    """

    path = Path(config_path).expanduser().parent / PACING_FILENAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, Mapping):
        return None
    pending = data.get("pending")
    if isinstance(pending, Mapping) and worker_name in pending:
        return None
    channels = data.get("channels")
    record = channels.get(worker_name) if isinstance(channels, Mapping) else None
    if not isinstance(record, Mapping):
        return None
    view = _channel_view(record, clock())
    view["state"] = "reconnecting"
    return view


__all__ = [
    "channel_reconnect_state",
    "credential_refusal",
    "CREDENTIAL_REFUSAL_REASONS",
    "RESTART_ATTEMPTED",
    "RESTART_KEPT_BACKOFF",
    "RESTART_PARKED_PERMANENT",
    "HANDSHAKE_RETRY_BASE_SECONDS",
    "HANDSHAKE_RETRY_CAP_SECONDS",
    "HANDSHAKE_RETRY_LIMIT",
    "HANDSHAKE_TIMEOUT_REASON",
    "CHANNEL_BACKOFF_BASE_SECONDS",
    "CHANNEL_BACKOFF_CAP_SECONDS",
    "HOST_QUIET_DEFAULT_SECONDS",
    "PACING_FILENAME",
    "RUNTIME_RETRY_BASE_SECONDS",
    "RUNTIME_RETRY_CAP_SECONDS",
    "RUNTIME_RETRY_LONG_SECONDS",
    "RUNTIME_RETRY_WINDOW_SECONDS",
    "RUNTIME_SCHEDULE",
    "RelayPacing",
    "rate_limit_wait",
]
