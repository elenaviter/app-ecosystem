"""Bounded source-only overhead witness; never connects to a live service.

Compare frozen production methods with current disabled/enabled methods,
alternating identical 100 ms synthetic IO cases, 20 samples per mode. This is
not a live performance guarantee. Run with explicit AE/SDK source overlays.
"""

import argparse
import ast
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import statistics
import subprocess
import time
from types import SimpleNamespace
import uuid

from app_foundation.data_bus import FederatedDataBusClient
from app_foundation.data_bus import client as clients
from project_board.client.message_timing import MessageTimingConfig, MessageTimingGate
from kdcube_ai_app.apps.chat.sdk.config_scopes import DataBusTimingConfig
from kdcube_ai_app.apps.chat.sdk.runtime.data_bus import timing, types, worker
from kdcube_ai_app.apps.chat.sdk.runtime.data_bus.stream import DataBusClaim


def baseline(root, commit, path, cls, method, namespace):
    source = subprocess.check_output(["git", "-C", str(root), "show", f"{commit}:{path}"], text=True)
    owner = next(node for node in ast.parse(source).body if isinstance(node, ast.ClassDef) and node.name == cls)
    fn = next(node for node in owner.body if isinstance(node, ast.AsyncFunctionDef) and node.name == method)
    module = ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))
    local = {}
    exec(compile(module, f"baseline:{commit}:{path}", "exec"), namespace, local)
    return local[method]


async def run(args):
    ae = Path(args.ae_root)
    kd = Path(args.kd_root)
    ae_base = "6d9d45336fd6cda7d8c2d41435329b7ab8dc3d6b"
    kd_base = "f8845c720981a8427ad5850afd91aa9e94647620"
    prefix = "app/ai-app/src/kdcube-ai-app/kdcube_ai_app/apps/chat/sdk/runtime/data_bus/"
    old_client = baseline(ae, ae_base, "packages/app-foundation/src/app_foundation/data_bus/client.py", "FederatedDataBusClient", "request", vars(clients))
    old_process = baseline(kd, kd_base, prefix + "worker.py", "DataBusBundleWorker", "_process_claim", vars(worker))
    old_invoke = baseline(kd, kd_base, prefix + "worker.py", "DataBusBundleWorker", "_invoke_handler", vars(worker))
    old_reply = baseline(kd, kd_base, prefix + "types.py", "DataBusReply", "event", vars(types))
    current_client = FederatedDataBusClient.request
    current_process = worker.DataBusBundleWorker._process_claim
    current_invoke = worker.DataBusBundleWorker._invoke_handler
    current_reply = types.DataBusReply.event
    disabled = timing.MemoryCapture()
    enabled = timing.MemoryCapture()
    expiry = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    enabled.configure(DataBusTimingConfig(enabled=True, expires_at=expiry))
    client_gate = MessageTimingGate()
    client_gate.configure(MessageTimingConfig(True, expiry))

    # Fake transport with a fixed delay, no socket, thread, file or remote IO.
    class Socket:
        connected = True

        async def call(self, event, data, timeout):
            await asyncio.sleep(0.1)
            message_id = data["messages"][0]["message_id"]
            bus._pending[message_id].set_result(clients.DataBusOutcome(message_id=message_id, status="ok", data={}, subject="s", object_ref="o"))
            return {"status": "accepted", "accepted": [{"message_id": message_id}]}

    from app_foundation.data_bus import DataBusClaim as ClientClaim
    bus = FederatedDataBusClient(platform_url="https://example.invalid", credential=ClientClaim(tenant="t", project="p", bundle_id="b", session_id="s", expires_at=2_000_000_000, federated_token="synthetic", partition_ref="o"), socket_factory=Socket)
    # Mark the synthetic client connected without opening any transport.
    bus.socket = Socket()
    bus._connected.set()

    class Bundle:
        async def handle(self, ctx, message):
            await asyncio.sleep(0.1)
            await ctx.reply.ok({})
            return {"status": "ok"}

    async def workflow(*args, **kwargs):
        return Bundle(), None

    async def noop(*args, **kwargs):
        return None

    worker.get_workflow_instance_async = workflow
    worker._refresh_bundle_props = noop
    worker._make_reply_comm = lambda **kwargs: SimpleNamespace(service_event=lambda **kwargs: None)
    worker.application_readiness_registry.require_ready = lambda **kwargs: None
    for name in ("make_local_bundle_operation_caller", "make_local_bundle_operation_stream_caller", "make_local_bundle_named_service_caller"):
        setattr(worker, name, lambda **kwargs: object())
    from kdcube_ai_app.apps.chat.ingress import resolvers
    resolvers.get_pg_pool = noop
    processor = object.__new__(worker.DataBusBundleWorker)
    processor.redis = None
    processor.relay = None
    processor.bundle_id = "b"
    processor.bundle_spec = object()
    processor.bundle_config = object()
    processor.bundle_allowed_roles = ()
    processor.handler_specs = {"s": types.DataBusHandlerSpec(method_name="handle", subject="s")}
    processor.stream = SimpleNamespace(write_result=noop, ack=noop)

    class Owner:
        count = 0
        def submit_record(self, record):
            self.count += 1
            return True

    # Admission cost, not physical writer latency, is in the client request.
    from project_board.client import relay_logging
    owner = Owner()
    relay_logging.relay_log_owner = lambda: owner
    result = {}
    for lane in ("client", "server"):
        samples = {mode: [] for mode in ("baseline", "disabled", "enabled")}
        for _ in range(20):
            for mode in samples:
                request_id = str(uuid.uuid4())
                if lane == "client":
                    FederatedDataBusClient.request = old_client if mode == "baseline" else current_client
                    options = {}
                    if mode == "enabled":
                        options["timing_observer"] = client_gate.observer("control.pull", request_id)
                    start = time.perf_counter()
                    await bus.request(subject="s", object_ref="o", payload={}, idempotency_key="k", message_id=request_id, **options)
                else:
                    timing.CAPTURE = worker.CAPTURE = enabled if mode == "enabled" else disabled
                    worker.DataBusBundleWorker._process_claim = old_process if mode == "baseline" else current_process
                    worker.DataBusBundleWorker._invoke_handler = old_invoke if mode == "baseline" else current_invoke
                    types.DataBusReply.event = old_reply if mode == "baseline" else current_reply
                    msg = types.DataBusMessage(message_id=request_id, tenant="t", project="p", bundle_id="b", subject="s", reply={"session_id": "s"})
                    start = time.perf_counter()
                    await processor._process_claim(DataBusClaim("s", "1-0", msg, {}, "c"))
                samples[mode].append(time.perf_counter() - start)
        medians = {mode: statistics.median(values) for mode, values in samples.items()}
        result[lane] = {"samples_per_mode": 20, "synthetic_io_seconds": 0.1, "median_seconds": medians, "overhead_percent": {mode: (medians[mode] / medians["baseline"] - 1) * 100 for mode in ("disabled", "enabled")}}
    result["client_records_admitted"] = owner.count
    result["server_claims_sampled"] = enabled.sampled_claims
    result["server_records_buffered"] = len(enabled._records)
    result["server_drops"] = enabled.dropped_records
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ae-root", required=True)
    parser.add_argument("--kd-root", required=True)
    asyncio.run(run(parser.parse_args()))
