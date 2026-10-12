"""Bounded synthetic queue delivery with busy sessions and real owned disk IO.

This runs the production side server, queues and fresh Card fences. Transport
is synthetic; it is not live admission, arbitrary host pressure or a restart.
"""
import asyncio
import json
import os
import threading
import time

import pytest

from project_board.client import host_config
from project_board.client.coordinate_queue import CoordinateQueue
from project_board.contract.worker_identity import WorkerSessionIdentity
from relay_helpers import LoopHeartbeat, StableClient, make_host, make_supervisor, submit_request
from test_relay_coordinate_beside_cycle import _session


@pytest.mark.parametrize('session_count, requests_per_session', [(8, 3), (16, 8)])
def test_concurrent_sessions_queue_behind_busy_peer_during_owned_disk_io(
    tmp_path, session_count, requests_per_session
):
    host, _identity, first = make_host(tmp_path)
    channels = [first]
    for index in range(1, session_count):
        channels.append(host_config.enroll_worker_channel(
            host.path,
            identity=WorkerSessionIdentity.create(
                'codex', f'{index + 1:08d}-1111-4111-8111-111111111111'),
            profile=f'problem-board-load-{index}', authorized=True,
        ))
    host = host_config.HostRelayConfig.load(host.path)
    root = host.connection_hub_state_root
    root.mkdir(parents=True, exist_ok=True)
    (root / 'profiles.json').write_text(json.dumps({'profiles': [
        {'name': channel.profile, 'endpoint': 'https://runtime.example/mcp',
         'credential_ref': f'synthetic-{index}', 'access_id': f'synthetic-card-{index}',
         'auth_type': 'oauth', 'record_version': 3, 'updated_at': 't0'}
        for index, channel in enumerate(channels)
    ]}), encoding='utf-8')
    queue = CoordinateQueue(host.field_root)
    supervisor = make_supervisor(host)
    requests = [(channel, submit_request(queue, channel))
                for channel in channels for _ in range(requests_per_session)]
    stop_io = threading.Event()
    io_started = threading.Event()
    writes = []

    def owned_io():
        # One owned file and at most 200 writes: no live queue or host-wide
        # stress command is touched, and the producer always joins below.
        with (tmp_path / 'synthetic-load.bin').open('wb') as stream:
            for _ in range(200):
                if stop_io.is_set():
                    break
                stream.seek(0)
                stream.write(b'x' * 65536)
                stream.flush()
                os.fsync(stream.fileno())
                writes.append(True)
                io_started.set()
                stop_io.wait(0.01)

    async def scenario():
        gate, entered = asyncio.Event(), asyncio.Event()

        class BusyClient(StableClient):
            async def action_with_transport_identity(self, **kwargs):
                entered.set()
                await gate.wait()
                return await super().action_with_transport_identity(**kwargs)

        clients = [BusyClient(), *[StableClient() for _ in channels[1:]]]
        for channel, client in zip(channels, clients):
            supervisor._sessions[channel.worker_name] = _session(
                channel, client, card_fingerprint=supervisor._card_fingerprint(host, channel))
        io_worker = threading.Thread(target=owned_io, name='synthetic-w758-io')
        io_worker.start()
        beat = LoopHeartbeat()
        beat.start()
        responses = {}

        async def collect_until(expected):
            deadline = time.monotonic() + 8
            while len(responses) < expected:
                assert time.monotonic() < deadline, 'bounded loaded queue delivery did not complete'
                for channel, request in requests:
                    key = request['request_id']
                    if key not in responses:
                        response = await asyncio.to_thread(
                            queue.take_response, worker_name=channel.worker_name, request_id=key)
                        if response is not None:
                            responses[key] = response
                await asyncio.sleep(0.02)

        try:
            assert await asyncio.to_thread(io_started.wait, 2), 'disk IO did not run'
            supervisor._ensure_coordinate_server()
            await asyncio.wait_for(entered.wait(), 3)
            await collect_until((session_count - 1) * requests_per_session)
            # The held peer's requests are durably retained while all other
            # sessions complete. No request is withdrawn or re-submitted.
            assert clients[0].calls == []
            assert all(queue.holds(worker_name=channel.worker_name,
                                   request_id=request['request_id'])
                       for channel, request in requests if channel == first)
            assert writes, 'the queue was not exercised beside actual disk writes'
            gate.set()
            await collect_until(session_count * requests_per_session)
            assert all(response['ok'] is True for response in responses.values())
            assert [len(client.calls) for client in clients] == [requests_per_session] * session_count
        finally:
            gate.set()
            stop_io.set()
            await asyncio.to_thread(io_worker.join, 3)
            assert not io_worker.is_alive(), 'owned disk IO thread did not stop'
            await supervisor.stop_coordinate_server()
            await beat.stop()
            await supervisor.aclose()
        assert beat.max_gap < 1.0, f'event loop stalled {beat.max_gap:.3f}s'
        print(f'sessions={session_count} requests={len(requests)} '
              f'responses_ok={len(responses)} retained_busy={requests_per_session} '
              f'io_writes={len(writes)} loop_max_gap_seconds={beat.max_gap:.6f}')

    asyncio.run(scenario())
