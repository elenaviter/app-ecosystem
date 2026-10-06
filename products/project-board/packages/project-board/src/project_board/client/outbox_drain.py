"""Notification-driven relay delivery for durable worker outbox rows."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Callable, Protocol, Sequence

from .host_config import HostRelayConfig, WorkerChannelConfig
from .local_wake import RelayOutboxWakeListener, notify_relay_outbox
from .outbox_store import OutboxStore
from .relay_pacing import RelayPacing


logger = logging.getLogger(__name__)


class _OutboxAdapter(Protocol):
    async def _flush_outbox(
        self,
        *,
        project_ref: str | None = None,
    ) -> dict[str, int]: ...


class OutboxSession(Protocol):
    adapter: _OutboxAdapter
    closing: bool


class RelayOutboxDrainServer:
    """Wake and drain worker-owned rows through an already-open Card channel."""

    SAFETY_PROBE_SECONDS = 30.0
    PROJECT_LIMIT = 20

    def __init__(
        self,
        *,
        config_path: str | Path,
        pacing: RelayPacing,
        session_for: Callable[[str], OutboxSession | None],
        session_matches: Callable[
            [HostRelayConfig, WorkerChannelConfig, OutboxSession], bool
        ],
        drain_lock: Callable[[str], asyncio.Lock],
        log: logging.Logger | None = None,
        card_fingerprint: Callable[[HostRelayConfig, WorkerChannelConfig], str] | None = None,
    ) -> None:
        self.config_path = Path(config_path)
        # Reads the channel's profile file; the off-loop pass calls it in its
        # thread and hands the result to session_matches (W456 criterion 4).
        self.card_fingerprint = card_fingerprint
        self.pacing = pacing
        self.session_for = session_for
        self.session_matches = session_matches
        self.drain_lock = drain_lock
        self.log = log or logger
        self.task: asyncio.Task | None = None
        self.draining: dict[str, asyncio.Task] = {}

    def ensure_started(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(
                self._serve(),
                name="problem-board-outbox-server",
            )

    async def stop(self) -> None:
        server = self.task
        self.task = None
        if server is not None and not server.done():
            try:
                host = HostRelayConfig.load(self.config_path)
            except Exception:  # noqa: BLE001 - cancellation remains authoritative
                pass
            else:
                # Release a listener blocked in the executor before cancelling
                # its coroutine, so shutdown does not wait for the safety probe.
                notify_relay_outbox(host.field_root)
            server.cancel()
            await asyncio.gather(server, return_exceptions=True)
        drains = [task for task in self.draining.values() if not task.done()]
        if drains:
            # A claimed row remains durable, but finishing the bounded send
            # avoids making a routine relay restart wait for lease recovery.
            await asyncio.gather(*drains, return_exceptions=True)
        self.draining.clear()

    async def _serve(self) -> None:
        """Bind before the first probe, then wait for durable-write hints."""

        while True:
            try:
                host = HostRelayConfig.load(self.config_path)
                break
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a later probe reloads config
                self.log.warning(
                    "Problem Board outbox server could not load the host config",
                    exc_info=True,
                )
                await asyncio.sleep(self.SAFETY_PROBE_SECONDS)
        # A write before bind is found by the first probe. A write after bind
        # leaves a byte for the following probe.
        with RelayOutboxWakeListener(host.field_root) as wake:
            while True:
                try:
                    await self.serve_once_off_loop()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - the next pass retries
                    self.log.warning(
                        "Problem Board outbox server pass failed",
                        exc_info=True,
                    )
                try:
                    await asyncio.to_thread(wake.wait, self.SAFETY_PROBE_SECONDS)
                except asyncio.CancelledError:
                    # asyncio.run() cancels tasks before it shuts down the
                    # default executor. Release the listener's worker thread
                    # here as well as in stop(), or loop shutdown waits out the
                    # complete safety-probe interval.
                    notify_relay_outbox(host.field_root)
                    raise

    def _candidates(self, host: HostRelayConfig) -> list[tuple[WorkerChannelConfig, OutboxSession]]:
        """Channels a drain may start for now: in-memory checks only, no file access."""

        found: list[tuple[WorkerChannelConfig, OutboxSession]] = []
        for channel in host.workers:
            name = channel.worker_name
            if channel.state != "active" or name in self.draining:
                continue
            if self.drain_lock(name).locked():
                continue
            session = self.session_for(name)
            if session is None or session.closing:
                continue
            if not self.pacing.channel_request_due(
                name,
                connected=bool(getattr(getattr(session.adapter, "client", None), "connected", False)),
            ):
                continue
            found.append((channel, session))
        return found

    def serve_once(self) -> list[str]:
        """Start one bounded ready-row drain per eligible worker channel."""

        host = HostRelayConfig.load(self.config_path)
        if self.pacing.host_quiet_seconds() > 0:
            return []
        outbox = OutboxStore(host.field_root / ".problem-board")
        ready = {
            channel.worker_name: outbox.ready_project_refs(
                worker_name=channel.worker_name, limit=self.PROJECT_LIMIT
            )
            for channel, _session in self._candidates(host)
        }
        return self._start_drains(host, ready)

    async def serve_once_off_loop(self) -> list[str]:
        """The server's pass: the config load and the ready-row scans run in a thread.

        W456 criterion 4 (dev-main, 2026-10-05): the scan's directory listing
        held the event loop 3.6 s. Eligibility is decided on the loop from
        memory, then checked again before a drain starts.
        """

        host = await asyncio.to_thread(HostRelayConfig.load, self.config_path)
        if self.pacing.host_quiet_seconds() > 0:
            return []
        names = [channel.worker_name for channel, _session in self._candidates(host)]
        if not names:
            return []

        channels = {channel.worker_name: channel for channel, _session in self._candidates(host)}

        def scan() -> tuple[dict[str, list[str]], dict[str, str]]:
            outbox = OutboxStore(host.field_root / ".problem-board")
            ready = {
                name: outbox.ready_project_refs(worker_name=name, limit=self.PROJECT_LIMIT)
                for name in names
            }
            cards = (
                {name: self.card_fingerprint(host, channels[name]) for name in names if ready[name] and name in channels}
                if self.card_fingerprint is not None
                else {}
            )
            return ready, cards

        ready, cards = await asyncio.to_thread(scan)
        return self._start_drains(host, ready, cards=cards if self.card_fingerprint is not None else None)

    def _start_drains(
        self,
        host: HostRelayConfig,
        ready: dict[str, list[str]],
        *,
        cards: dict[str, str] | None = None,
    ) -> list[str]:
        started: list[str] = []
        for channel, session in self._candidates(host):
            name = channel.worker_name
            project_refs = ready.get(name) or []
            if not project_refs:
                continue
            if cards is None:
                matches = self.session_matches(host, channel, session)
            elif name in cards:
                matches = self.session_matches(host, channel, session, card=cards[name])
            else:
                continue  # became eligible after the scan: the next pass takes it
            if not matches:
                continue
            task = asyncio.create_task(
                self._drain(
                    channel,
                    session,
                    project_refs,
                    field_root=host.field_root,
                ),
                name=f"problem-board-outbox-{name}",
            )
            self.draining[name] = task
            started.append(name)
        return started

    async def _drain(
        self,
        channel: WorkerChannelConfig,
        session: OutboxSession,
        project_refs: Sequence[str],
        *,
        field_root: Path,
    ) -> None:
        completed = False
        try:
            for project_ref in project_refs[: self.PROJECT_LIMIT]:
                if session.closing:
                    return
                await session.adapter._flush_outbox(project_ref=project_ref)
            completed = True
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - leases and the cycle are recovery
            self.log.warning(
                "Problem Board outbox drain beside the cycle failed worker=%s",
                channel.worker_name,
                exc_info=True,
            )
        finally:
            current = asyncio.current_task()
            if self.draining.get(channel.worker_name) is current:
                self.draining.pop(channel.worker_name, None)
                if completed:
                    # A write wake can be consumed while this worker is still
                    # draining, and one flush claims at most 20 rows. Wake only
                    # after removing the marker so the next pass can claim any
                    # durable remainder immediately.
                    notify_relay_outbox(field_root)


__all__ = ["OutboxSession", "RelayOutboxDrainServer"]
