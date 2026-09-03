from __future__ import annotations

import time
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
from kdcube_cli.control import (
    ApplicationRef,
    ControlEvent,
    DeploymentTargetRef,
    EndpointDeploymentTarget,
    KDCubeControlError,
    LocalDeploymentTarget,
    LocalInitializationRequest,
    LocalPlatformSourceRequest,
    LocalStartRequest,
    LocalStopRequest,
    SurfaceKind,
    SurfaceSelector,
    select_local_target,
)

from connection_hub_cli.errors import HostControlError
from connection_hub_cli.management.models import ManagementTarget
from connection_hub_cli.models import HostSelection
from connection_hub_cli.state import HostStore

TargetFactory = Callable[[HostSelection], Any]
SurfaceProbe = Callable[[str], bool]
BrowserOpener = Callable[[str], bool]


def _event_record(event: ControlEvent) -> dict[str, str]:
    return {"kind": event.kind.value, "message": event.message}


def _safe_target_error(exc: KDCubeControlError) -> HostControlError:
    return HostControlError(f"kdcube.{exc.code.value}", exc.summary)


def _default_target_factory(selection: HostSelection):
    if selection.kind == "local":
        assert selection.workdir is not None
        return LocalDeploymentTarget(
            DeploymentTargetRef.local(
                Path(selection.workdir),
                tenant=selection.tenant,
                project=selection.project,
            )
        )
    assert selection.endpoint is not None
    return EndpointDeploymentTarget(
        DeploymentTargetRef.endpoint_target(
            selection.endpoint,
            tenant=selection.tenant,
            project=selection.project,
        )
    )


def _default_surface_probe(url: str) -> bool:
    try:
        with httpx2.Client(
            timeout=5.0,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = client.get(url)
    except httpx2.HTTPError:
        return False
    return response.status_code != 404 and response.status_code < 500


class HostService:
    def __init__(
        self,
        *,
        store: HostStore,
        target_factory: TargetFactory = _default_target_factory,
        surface_probe: SurfaceProbe = _default_surface_probe,
        opener: BrowserOpener = webbrowser.open,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.store = store
        self.target_factory = target_factory
        self.surface_probe = surface_probe
        self.opener = opener
        self.sleeper = sleeper

    def _require_selection(self) -> HostSelection:
        selection = self.store.get()
        if selection is None:
            raise HostControlError(
                "host_not_selected",
                "No Connection Hub application host is selected. Run connection-hub setup.",
            )
        return selection

    @staticmethod
    def _selection_for_existing_local(workdir: Path) -> HostSelection:
        try:
            reference = select_local_target(workdir, require_existing=True)
        except KDCubeControlError as exc:
            raise _safe_target_error(exc) from exc
        if not reference.tenant or not reference.project or reference.workdir is None:
            raise HostControlError(
                "host_coordinates_missing",
                "The selected KDCube runtime has no complete tenant and project coordinates.",
            )
        return HostSelection.local(
            workdir=str(reference.workdir),
            tenant=reference.tenant,
            project=reference.project,
        )

    def _surface_details(self, selection: HostSelection, target: Any) -> dict[str, Any]:
        application = ApplicationRef(selection.application_id)
        widget = target.resolve_surface(
            application,
            SurfaceSelector(kind=SurfaceKind.WIDGET, alias=selection.widget_alias),
        )
        mcp = target.resolve_surface(
            application,
            SurfaceSelector(kind=SurfaceKind.MCP, alias=selection.mcp_alias),
        )
        return {
            "application_id": selection.application_id,
            "widget_url": widget.url,
            "mcp_url": mcp.url,
            "declared": {"widget": widget.declared, "mcp": mcp.declared},
        }

    def _inspect(self, selection: HostSelection, *, probe: bool) -> dict[str, Any]:
        target = self.target_factory(selection)
        try:
            status = target.status() if selection.kind == "local" else target.describe()
            surfaces = self._surface_details(selection, target)
        except KDCubeControlError as exc:
            raise _safe_target_error(exc) from exc
        surface_ready = None
        if probe:
            probe_results = [
                self.surface_probe(surfaces[key]) for key in ("widget_url", "mcp_url")
            ]
            surface_ready = all(probe_results)
        return {
            "selected": True,
            "target": selection.to_dict(),
            "runtime": {
                "reachable": status.reachable,
                "initialized": status.initialized,
                "running": status.running,
                "platform_ref": status.release.platform_ref,
                "diagnostics": [
                    {
                        "code": item.code,
                        "severity": item.severity.value,
                        "summary": item.summary,
                        "recovery": dict(item.recovery),
                    }
                    for item in status.diagnostics
                ],
            },
            "application": surfaces,
            "surfaces_ready": surface_ready,
        }

    def status(self, *, probe: bool = False) -> dict[str, Any]:
        selection = self.store.get()
        if selection is None:
            return {"selected": False}
        return self._inspect(selection, probe=probe)

    def management_target(self) -> ManagementTarget:
        selection = self._require_selection()
        target = self.target_factory(selection)
        try:
            surface = target.resolve_surface(
                ApplicationRef(selection.application_id),
                SurfaceSelector(
                    kind=SurfaceKind.WIDGET,
                    alias=selection.widget_alias,
                ),
            )
        except KDCubeControlError as exc:
            raise _safe_target_error(exc) from exc
        if not surface.route or not surface.url.endswith(surface.route):
            raise HostControlError(
                "host_public_base_unavailable",
                "The selected KDCube host has no usable public base URL.",
            )
        return ManagementTarget.create(
            public_base_url=surface.url[: -len(surface.route)],
            tenant=selection.tenant,
            project=selection.project,
            session_target_key=selection.target_key,
        )

    def _wait_until_ready(
        self,
        selection: HostSelection,
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            inspection = self._inspect(selection, probe=True)
            if inspection["surfaces_ready"]:
                return inspection
            if time.monotonic() >= deadline:
                raise HostControlError(
                    "host_not_ready",
                    "The Connection Hub application routes did not become ready before the timeout.",
                )
            self.sleeper(min(2.0, max(0.0, deadline - time.monotonic())))

    def setup_existing_local(
        self,
        *,
        workdir: Path,
        replace: bool,
        start: bool,
        open_browser: bool,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        selection = self._selection_for_existing_local(workdir)
        target = self.target_factory(selection)
        events: list[dict[str, str]] = []
        try:
            self._surface_details(selection, target)
            if start:
                target.start(
                    LocalStartRequest(),
                    event_sink=lambda event: events.append(_event_record(event)),
                )
        except KDCubeControlError as exc:
            raise _safe_target_error(exc) from exc
        inspection = (
            self._wait_until_ready(selection, timeout_seconds=timeout_seconds)
            if start
            else self._inspect(selection, probe=False)
        )
        changed = self.store.put(selection, replace=replace)
        if open_browser:
            self._open_selection(selection)
        return {"changed": changed, "events": events, "host": inspection}

    def setup_new_local(
        self,
        *,
        runtime_root: Path,
        tenant: str,
        project: str,
        repository: str,
        release_ref: str | None,
        upstream: bool,
        build: bool,
        auth_type: str,
        google_client_id: str | None,
        bootstrap_admin_email: str | None,
        replace: bool,
        start: bool,
        open_browser: bool,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        try:
            reference = select_local_target(
                runtime_root,
                tenant=tenant,
                project=project,
                require_existing=False,
            )
        except KDCubeControlError as exc:
            raise _safe_target_error(exc) from exc
        assert reference.workdir is not None
        selection = HostSelection.local(
            workdir=str(reference.workdir), tenant=tenant, project=project
        )
        target = self.target_factory(selection)
        events: list[dict[str, str]] = []

        def record_event(event: ControlEvent) -> None:
            events.append(_event_record(event))

        try:
            prepared = target.prepare_source(
                LocalPlatformSourceRequest(
                    repository=repository,
                    release_ref=release_ref,
                    upstream=upstream,
                    build=build,
                ),
                event_sink=record_event,
            )
            target.initialize(
                LocalInitializationRequest(
                    descriptor_source=prepared.descriptor_source,
                    install_mode=prepared.install_mode,
                    release_ref=prepared.release_ref,
                    parameterize_defaults=True,
                    auth_type=auth_type,
                    auth_provider="google" if auth_type == "bundle" else None,
                    auth_client_id=google_client_id,
                    bootstrap_admin_email=bootstrap_admin_email,
                ),
                event_sink=record_event,
            )
            self._surface_details(selection, target)
            if start:
                target.start(
                    LocalStartRequest(build=build),
                    event_sink=record_event,
                )
        except KDCubeControlError as exc:
            raise _safe_target_error(exc) from exc
        inspection = (
            self._wait_until_ready(selection, timeout_seconds=timeout_seconds)
            if start
            else self._inspect(selection, probe=False)
        )
        changed = self.store.put(selection, replace=replace)
        if open_browser:
            self._open_selection(selection)
        return {
            "changed": changed,
            "events": events,
            "prepared_release": prepared.release_ref,
            "host": inspection,
        }

    def setup_endpoint(
        self,
        *,
        endpoint: str,
        tenant: str,
        project: str,
        replace: bool,
        open_browser: bool,
    ) -> dict[str, Any]:
        selection = HostSelection.endpoint_target(
            endpoint=endpoint, tenant=tenant, project=project
        )
        inspection = self._inspect(selection, probe=True)
        if not inspection["surfaces_ready"]:
            raise HostControlError(
                "host_not_ready",
                "The selected Connection Hub application routes are not reachable.",
            )
        changed = self.store.put(selection, replace=replace)
        if open_browser:
            self._open_selection(selection)
        return {"changed": changed, "host": inspection}

    def start(self, *, build: bool, timeout_seconds: float) -> dict[str, Any]:
        selection = self._require_selection()
        target = self.target_factory(selection)
        events: list[dict[str, str]] = []
        try:
            result = target.start(
                LocalStartRequest(build=build),
                event_sink=lambda event: events.append(_event_record(event)),
            )
        except KDCubeControlError as exc:
            raise _safe_target_error(exc) from exc
        return {
            "operation": result.operation,
            "changed": result.changed,
            "events": events,
            "host": self._wait_until_ready(selection, timeout_seconds=timeout_seconds),
        }

    def stop(self, *, remove_volumes: bool) -> dict[str, Any]:
        selection = self._require_selection()
        target = self.target_factory(selection)
        events: list[dict[str, str]] = []
        try:
            result = target.stop(
                LocalStopRequest(remove_volumes=remove_volumes),
                event_sink=lambda event: events.append(_event_record(event)),
            )
        except KDCubeControlError as exc:
            raise _safe_target_error(exc) from exc
        return {
            "operation": result.operation,
            "changed": result.changed,
            "events": events,
            "running": result.running,
        }

    def _open_selection(self, selection: HostSelection) -> dict[str, Any]:
        target = self.target_factory(selection)
        try:
            result = target.open_application(
                ApplicationRef(selection.application_id),
                SurfaceSelector(kind=SurfaceKind.WIDGET, alias=selection.widget_alias),
                opener=self.opener,
            )
        except KDCubeControlError as exc:
            raise _safe_target_error(exc) from exc
        return {"operation": result.operation, "url": result.url}

    def open(self) -> dict[str, Any]:
        return self._open_selection(self._require_selection())

    def mcp_endpoint(self) -> str:
        status = self._inspect(self._require_selection(), probe=False)
        return str(status["application"]["mcp_url"])

    def diagnostics(self, *, probe: bool) -> list[dict[str, Any]]:
        if self.store.get() is None:
            return [
                {
                    "code": "host_not_selected",
                    "severity": "warning",
                    "summary": "No Connection Hub application host is selected.",
                    "recovery": "Run: connection-hub setup",
                }
            ]
        try:
            status = self.status(probe=probe)
        except HostControlError as exc:
            return [
                {
                    "code": exc.code,
                    "severity": "error",
                    "summary": exc.message,
                    "recovery": "Run: connection-hub setup --replace",
                }
            ]
        diagnostics = list(status["runtime"]["diagnostics"])
        diagnostics.append(
            {
                "code": "host_application_routes_resolved",
                "severity": "ok",
                "summary": (
                    "The CLI constructed the Connection Hub browser and MCP URLs "
                    "from the selected KDCube target coordinates."
                ),
                "recovery": None,
            }
        )
        if probe:
            diagnostics.append(
                {
                    "code": (
                        "host_surfaces_ready"
                        if status["surfaces_ready"]
                        else "host_surfaces_unreachable"
                    ),
                    "severity": "ok" if status["surfaces_ready"] else "error",
                    "summary": (
                        "The Connection Hub browser and MCP routes are reachable."
                        if status["surfaces_ready"]
                        else "The Connection Hub browser or MCP route is unavailable."
                    ),
                    "recovery": (
                        None
                        if status["surfaces_ready"]
                        else "Start or repair the selected application host, then retry."
                    ),
                }
            )
        return diagnostics
