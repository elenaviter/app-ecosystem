from __future__ import annotations

import hashlib
from typing import Any, Mapping


WORKER_STREAM_EVENT = "problem_board.worker.event.v1"
WORKER_STREAM_PARTITION_PREFIX = "work:worker-stream:"


def worker_stream_partition(principal_key: str) -> str:
    principal = str(principal_key or "").strip()
    if not principal:
        raise ValueError("A worker-stream principal is required.")
    digest = hashlib.sha256(principal.encode("utf-8")).hexdigest()[:32]
    return f"{WORKER_STREAM_PARTITION_PREFIX}{digest}"


class ProblemBoardWorkerStream:
    """Address compact wake events to live worker-card Data Bus sessions."""

    def __init__(
        self,
        *,
        redis: Any,
        tenant: str,
        project: str,
        bundle_id: str,
        publisher: Any | None = None,
    ) -> None:
        if publisher is None:
            from kdcube_ai_app.apps.chat.sdk.runtime.data_bus.live_sessions import (
                DataBusLiveSessionPublisher,
            )

            publisher = DataBusLiveSessionPublisher(
                redis=redis,
                tenant=tenant,
                project=project,
                bundle_id=bundle_id,
            )
        self.publisher = publisher

    async def connected(self, principal_key: str) -> bool:
        return await self.publisher.connected(str(principal_key or ""))

    async def notify(
        self,
        worker: Mapping[str, Any],
        *,
        kind: str,
        refs: Mapping[str, Any] | None = None,
    ) -> int:
        principal = str(worker.get("principal_key") or "").strip()
        if not principal:
            return 0
        data = {
            "schema": WORKER_STREAM_EVENT,
            "kind": str(kind or "state.changed"),
            "worker_ref": str(worker.get("worker_ref") or ""),
            "worker_name": str(worker.get("worker_name") or ""),
            "refs": {
                str(key): str(value)
                for key, value in dict(refs or {}).items()
                if str(key) and value not in (None, "")
            },
        }
        return await self.publisher.publish(
            principal=principal,
            event_type=WORKER_STREAM_EVENT,
            data=data,
        )


__all__ = [
    "ProblemBoardWorkerStream",
    "WORKER_STREAM_EVENT",
    "WORKER_STREAM_PARTITION_PREFIX",
    "worker_stream_partition",
]
