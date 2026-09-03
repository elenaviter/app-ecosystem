from __future__ import annotations

from pathlib import Path

import pytest
from kdcube_cli.control import (
    ApplicationSurface,
    ControlEvent,
    ControlEventKind,
    OperationResult,
    PreparedPlatformSource,
    ReleaseCoordinates,
    SurfaceKind,
    TargetCapabilities,
    TargetCapability,
    TargetStatus,
    UnsupportedCapabilityError,
)

from connection_hub_cli.errors import HostControlError
from connection_hub_cli.host import HostService
from connection_hub_cli.models import HostSelection, endpoint_address_kind
from connection_hub_cli.state import HostStore


class FakeTarget:
    def __init__(self, selection: HostSelection, root: Path) -> None:
        self.selection = selection
        self.root = root
        self.calls: list[tuple[str, object]] = []
        self.running = False

    def status(self):
        return self._status()

    def describe(self):
        return self._status()

    def _status(self):
        from kdcube_cli.control import DeploymentTargetRef

        reference = (
            DeploymentTargetRef.local(
                Path(self.selection.workdir),
                tenant=self.selection.tenant,
                project=self.selection.project,
            )
            if self.selection.kind == "local"
            else DeploymentTargetRef.endpoint_target(
                self.selection.endpoint,
                tenant=self.selection.tenant,
                project=self.selection.project,
            )
        )
        return TargetStatus(
            reference=reference,
            capabilities=TargetCapabilities(
                frozenset(
                    {
                        TargetCapability.STATUS,
                        TargetCapability.START,
                        TargetCapability.STOP,
                        TargetCapability.RESOLVE_ENDPOINTS,
                        TargetCapability.OPEN,
                    }
                )
            ),
            reachable=True,
            initialized=self.selection.kind == "local",
            running=self.running if self.selection.kind == "local" else None,
            release=ReleaseCoordinates(platform_ref="2026.09.02.1429"),
            diagnostics=(),
        )

    def resolve_surface(self, application, selector):
        base = self.selection.endpoint or "http://localhost:5173"
        route_kind = "widgets" if selector.kind == SurfaceKind.WIDGET else "mcp"
        route = (
            f"/api/integrations/bundles/{self.selection.tenant}/"
            f"{self.selection.project}/{application.bundle_id}/public/"
            f"{route_kind}/{selector.alias}"
        )
        return ApplicationSurface(
            application=application,
            kind=selector.kind,
            alias=selector.alias,
            route=route,
            url=f"{base}{route}",
            declared=self.selection.kind == "local",
            openable=selector.kind == SurfaceKind.WIDGET,
        )

    def prepare_source(self, request, *, event_sink=None):
        self.calls.append(("prepare_source", request))
        if event_sink is not None:
            event_sink(
                ControlEvent(kind=ControlEventKind.PROGRESS, message="source ready")
            )
        descriptor_source = self.root / "repo" / "app" / "ai-app" / "deployment"
        descriptor_source.mkdir(parents=True, exist_ok=True)
        return PreparedPlatformSource(
            repo_root=self.root / "repo",
            descriptor_source=descriptor_source,
            release_ref=request.release_ref or "2026.09.02.1429",
            install_mode="release",
        )

    def initialize(self, request, *, event_sink=None):
        self.calls.append(("initialize", request))
        if event_sink is not None:
            event_sink(
                ControlEvent(kind=ControlEventKind.PROGRESS, message="runtime prepared")
            )
        return OperationResult(
            target=self._status().reference,
            operation="initialize",
            changed=True,
            running=False,
        )

    def start(self, request, *, event_sink=None):
        self.calls.append(("start", request))
        if self.selection.kind == "endpoint":
            raise UnsupportedCapabilityError("endpoint:test", "start")
        self.running = True
        if event_sink is not None:
            event_sink(ControlEvent(kind=ControlEventKind.COMMAND, message="start"))
        return OperationResult(
            target=self._status().reference,
            operation="start",
            changed=True,
            running=True,
        )

    def stop(self, request, *, event_sink=None):
        self.calls.append(("stop", request))
        if self.selection.kind == "endpoint":
            raise UnsupportedCapabilityError("endpoint:test", "stop")
        self.running = False
        return OperationResult(
            target=self._status().reference,
            operation="stop",
            changed=True,
            running=False,
        )

    def open_application(self, application, selector, *, opener):
        surface = self.resolve_surface(application, selector)
        assert opener(surface.url)
        return OperationResult(
            target=self._status().reference,
            operation="open",
            changed=False,
            url=surface.url,
        )


class FakeTargets:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.targets: dict[str, FakeTarget] = {}

    def __call__(self, selection: HostSelection) -> FakeTarget:
        return self.targets.setdefault(
            selection.target_key, FakeTarget(selection, self.root)
        )


def _service(tmp_path: Path, *, probes=None, opened=None):
    probes = probes if probes is not None else []
    opened = opened if opened is not None else []
    targets = FakeTargets(tmp_path)
    service = HostService(
        store=HostStore(tmp_path / "state" / "host.json"),
        target_factory=targets,
        surface_probe=lambda url: probes.append(url) is None,
        opener=lambda url: opened.append(url) is None,
        sleeper=lambda _seconds: None,
    )
    return service, targets


@pytest.mark.parametrize(
    ("endpoint", "kind"),
    [
        ("http://localhost:5173", "loopback"),
        ("http://127.0.0.1:5173", "loopback"),
        ("http://[::1]:5173", "loopback"),
        ("https://192.0.2.10", "ip"),
        ("https://hub.example", "dns"),
    ],
)
def test_endpoint_address_kind_is_separate_from_control_kind(endpoint, kind):
    selection = HostSelection.endpoint_target(
        endpoint=endpoint, tenant="acme", project="prod", now="created"
    )

    assert selection.kind == "endpoint"
    assert endpoint_address_kind(endpoint) == kind
    assert selection.to_dict()["address"]["kind"] == kind


def test_endpoint_setup_probes_both_routes_and_records_nonsecret_target(
    tmp_path: Path,
) -> None:
    probes: list[str] = []
    opened: list[str] = []
    service, _targets = _service(tmp_path, probes=probes, opened=opened)

    result = service.setup_endpoint(
        endpoint="https://hub.example",
        tenant="acme",
        project="prod",
        replace=False,
        open_browser=True,
    )

    assert result["changed"] is True
    assert len(probes) == 2
    assert probes[0].endswith("/public/widgets/connections_settings")
    assert probes[1].endswith("/public/mcp/remote_mcp_proxy")
    assert opened == [probes[0]]
    assert service.mcp_endpoint() == probes[1]
    assert service.status()["target"]["address"] == {
        "host": "hub.example",
        "kind": "dns",
    }
    route_diagnostic = next(
        item
        for item in service.diagnostics(probe=False)
        if item["code"] == "host_application_routes_resolved"
    )
    assert "constructed" in route_diagnostic["summary"]
    assert "browser and MCP URLs" in route_diagnostic["summary"]
    management = service.management_target()
    assert management.public_base_url == "https://hub.example"
    assert management.resource == "urn:kdcube:management:deployment:acme:prod"
    assert management.session_target_key == service.store.get().target_key


def test_new_local_setup_uses_kdcube_source_initialize_and_lifecycle(
    tmp_path: Path,
) -> None:
    service, targets = _service(tmp_path)

    result = service.setup_new_local(
        runtime_root=tmp_path / "runtimes",
        tenant="local",
        project="connection-hub",
        repository="https://github.com/kdcube/kdcube.git",
        release_ref=None,
        upstream=False,
        build=False,
        auth_type="bundle",
        google_client_id="client.apps.googleusercontent.com",
        bootstrap_admin_email="admin@example.com",
        replace=False,
        start=True,
        open_browser=False,
        timeout_seconds=0,
    )

    assert result["prepared_release"] == "2026.09.02.1429"
    assert result["events"] == [
        {"kind": "progress", "message": "source ready"},
        {"kind": "progress", "message": "runtime prepared"},
        {"kind": "command", "message": "start"},
    ]
    selection = service.store.get()
    assert selection is not None
    assert Path(selection.workdir).name == "local__connection_hub"
    target = targets.targets[selection.target_key]
    assert [name for name, _request in target.calls] == [
        "prepare_source",
        "initialize",
        "start",
    ]
    initialize_request = target.calls[1][1]
    assert initialize_request.auth_type == "bundle"
    assert initialize_request.auth_provider == "google"
    assert initialize_request.auth_client_id == "client.apps.googleusercontent.com"
    assert initialize_request.bootstrap_admin_email == "admin@example.com"
    assert initialize_request.parameterize_defaults is True


def test_remote_host_lifecycle_fails_without_remote_operator_api(
    tmp_path: Path,
) -> None:
    service, _targets = _service(tmp_path)
    service.setup_endpoint(
        endpoint="https://hub.example",
        tenant="acme",
        project="prod",
        replace=False,
        open_browser=False,
    )

    with pytest.raises(HostControlError) as captured:
        service.start(build=False, timeout_seconds=0)

    assert captured.value.code == "kdcube.target.unsupported_capability"


def test_host_store_requires_explicit_replacement(tmp_path: Path) -> None:
    store = HostStore(tmp_path / "state" / "host.json")
    local = HostSelection.local(
        workdir=str(tmp_path / "local__hub"),
        tenant="local",
        project="hub",
        now="first",
    )
    remote = HostSelection.endpoint_target(
        endpoint="https://hub.example",
        tenant="acme",
        project="prod",
        now="second",
    )
    assert store.put(local) is True

    with pytest.raises(HostControlError):
        store.put(remote)

    assert store.get().target_key == local.target_key
    assert store.put(remote, replace=True) is True
    assert store.get().target_key == remote.target_key
