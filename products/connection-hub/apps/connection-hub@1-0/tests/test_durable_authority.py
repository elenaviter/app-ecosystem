from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.authority_config import (
    CONNECTION_HUB_AUTHORITY_FAMILIES,
)
from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import (
    load_dynamic_module_for_path,
)


def _entrypoint_module():
    bundle_root = Path(__file__).resolve().parents[1]
    _name, module = load_dynamic_module_for_path(bundle_root / "entrypoint.py")
    return module


class _PreparedStore:
    def __init__(self, events: list[object], name: str) -> None:
        self._events = events
        self._name = name

    async def ensure_schema(self) -> None:
        self._events.append(("schema", self._name))


class _CardHandles(_PreparedStore):
    async def reconcile_cleanup(self, *, limit: int) -> None:
        self._events.append(("card_cleanup", limit))


class _ReplayClaims(_PreparedStore):
    async def purge_expired(self, *, limit: int) -> int:
        self._events.append(("replay_cleanup", limit))
        return 0


class _Cutovers(_PreparedStore):
    def __init__(
        self,
        events: list[object],
        *,
        failure: Exception | None = None,
    ) -> None:
        super().__init__(events, "cutovers")
        self._failure = failure

    async def require_activated(
        self,
        generation_id: str,
        *,
        required_families: tuple[str, ...],
    ) -> object:
        self._events.append(
            ("require_activated", generation_id, required_families)
        )
        if self._failure is not None:
            raise self._failure
        return object()


def _authority(module, *, cutover_failure: Exception | None = None):
    events: list[object] = []
    authority = module.ConnectionHubDurableAuthority(
        config=SimpleNamespace(generation_id="durable-authority-v1"),
        oauth=_PreparedStore(events, "oauth"),
        card_handles=_CardHandles(events, "card_handles"),
        admission_replay=_ReplayClaims(events, "admission_replay"),
        cutovers=_Cutovers(events, failure=cutover_failure),
    )
    return authority, events


@pytest.mark.asyncio
async def test_prepare_requires_the_exact_complete_cutover_before_cleanup() -> None:
    module = _entrypoint_module()
    authority, events = _authority(module)

    await authority.prepare()

    assert events == [
        ("schema", "oauth"),
        ("schema", "card_handles"),
        ("schema", "admission_replay"),
        ("schema", "cutovers"),
        (
            "require_activated",
            "durable-authority-v1",
            CONNECTION_HUB_AUTHORITY_FAMILIES,
        ),
        ("card_cleanup", 100),
        ("replay_cleanup", 1000),
    ]


@pytest.mark.asyncio
async def test_prepare_fails_closed_when_the_cutover_receipt_is_missing() -> None:
    module = _entrypoint_module()
    failure = RuntimeError("authority_cutover_not_activated")
    authority, events = _authority(module, cutover_failure=failure)

    with pytest.raises(RuntimeError, match="authority_cutover_not_activated"):
        await authority.prepare()

    assert ("card_cleanup", 100) not in events
    assert ("replay_cleanup", 1000) not in events
    with pytest.raises(RuntimeError, match="authority_cutover_not_activated"):
        await authority.ensure_ready()


@pytest.mark.asyncio
async def test_readiness_is_verified_from_durable_evidence_on_every_call() -> None:
    module = _entrypoint_module()
    authority, events = _authority(module)

    await authority.ensure_ready()
    await authority.ensure_ready()

    assert events == [
        (
            "require_activated",
            "durable-authority-v1",
            CONNECTION_HUB_AUTHORITY_FAMILIES,
        ),
        (
            "require_activated",
            "durable-authority-v1",
            CONNECTION_HUB_AUTHORITY_FAMILIES,
        ),
    ]
