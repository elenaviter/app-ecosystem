from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from project_board.client import (
    cli,
    code_entrypoint,
    entrypoint,
    release_install,
    relay_source,
    source_control,
    source_composite,
)
from project_board.client.source_control import ClientSourceController
from project_board.client.source_manifest import (
    APP_ECOSYSTEM_SOURCE_PATHS,
    CLIENT_SOURCE_IMPORTS,
    KDCUBE_SOURCE_PATHS,
)
from project_board.contract.errors import DomainError


def test_source_control_reexports_the_code_entrypoint_contract() -> None:
    assert source_control.PROJECT_BOARD_CODE_ENTRYPOINT == (
        relay_source.PROJECT_BOARD_CODE_ENTRYPOINT
    )


@pytest.fixture(autouse=True)
def _complete_candidate_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Controller tests isolate switching from the real pip installer."""

    def install(**kwargs):
        release = release_install.release_path(kwargs["root"], kwargs["release_id"])
        environment = release / "venv"
        commands = environment / "bin"
        commands.mkdir(parents=True, exist_ok=True)
        for name in ("python", "pb"):
            command = commands / name
            command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            command.chmod(0o755)
        record = {
            "schema": release_install.INSTALLATION_SCHEMA,
            "release_id": kwargs["release_id"],
            "source": dict(kwargs["source"]),
            "installed_at": "2026-09-25T00:00:00Z",
            "activated_at": "",
            "environment": {
                "path": str(environment),
                "python": str(commands / "python"),
                "pb": str(commands / "pb"),
                "project_board_version": kwargs.get(
                    "expected_project_board_version", ""
                ),
                "imports": list(
                    kwargs.get(
                        "smoke_imports",
                        release_install.DEFAULT_IMPORT_SMOKE,
                    )
                ),
            },
            "launcher_version": release_install.LAUNCHER_VERSION,
        }
        release_install._atomic_write_json(
            release / release_install.INSTALLATION_MARKER,
            record,
        )
        return record

    monkeypatch.setattr(source_control, "install_release_environment", install)


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _source_repository(tmp_path: Path) -> tuple[Path, str, tuple[str, ...]]:
    repository = tmp_path / "app-ecosystem"
    repository.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "worker@example.test"),
        ("config", "user.name", "Worker"),
    ):
        _git(repository, *args)

    paths = APP_ECOSYSTEM_SOURCE_PATHS
    entrypoint = f"{paths[0]}/src/project_board/client/code_entrypoint.py"
    for path in paths:
        source = repository / path / "src"
        source.mkdir(parents=True)
        (source / "owned.py").write_text(f"SOURCE = {path!r}\n", encoding="utf-8")
    (repository / entrypoint).parent.mkdir(parents=True, exist_ok=True)
    (repository / entrypoint).write_text("print('pinned')\n", encoding="utf-8")
    (repository / "outside.txt").write_text("not exported\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "source")
    return repository, entrypoint, paths


def _kdcube_repository(tmp_path: Path) -> tuple[Path, tuple[str, ...]]:
    repository = tmp_path / "kdcube"
    repository.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "worker@example.test"),
        ("config", "user.name", "Worker"),
    ):
        _git(repository, *args)
    for path in KDCUBE_SOURCE_PATHS:
        source = repository / path / "src"
        source.mkdir(parents=True)
        (source / "owned.py").write_text(f"SOURCE = {path!r}\n", encoding="utf-8")
    (repository / "outside.txt").write_text("not exported\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "source")
    return repository, KDCUBE_SOURCE_PATHS


def test_code_release_contains_every_client_package_from_both_commits(
    tmp_path: Path,
) -> None:
    repository, entrypoint, paths = _source_repository(tmp_path)
    commit = _git(repository, "rev-parse", "HEAD")
    kdcube_repository, kdcube_paths = _kdcube_repository(tmp_path)
    kdcube_commit = _git(kdcube_repository, "rev-parse", "HEAD")
    root = tmp_path / "client-source"

    release = source_composite.export_client_release(
        app_ecosystem_repository=repository,
        app_ecosystem_ref=commit,
        kdcube_repository=kdcube_repository,
        kdcube_ref=kdcube_commit,
        root=root,
    )

    assert release.commit == commit
    assert release.entrypoint_path == entrypoint
    assert release.source_paths == paths + kdcube_paths
    assert len(release.release_id) == 64
    assert release.script.read_text(encoding="utf-8") == "print('pinned')\n"
    assert set(release.subtrees) == set(paths)
    assert not (release.path / "outside.txt").exists()
    marker = json.loads((release.path / relay_source.RELEASE_MARKER).read_text())
    assert marker["release_id"] == release.release_id
    assert marker["source_paths"] == list(paths + kdcube_paths)
    assert {component["name"] for component in marker["components"]} == {
        "app_ecosystem",
        "kdcube",
    }


def test_kdcube_commit_changes_the_composite_release_id(tmp_path: Path) -> None:
    repository, _entrypoint, _paths = _source_repository(tmp_path)
    commit = _git(repository, "rev-parse", "HEAD")
    kdcube_repository, kdcube_paths = _kdcube_repository(tmp_path)
    first_kdcube_commit = _git(kdcube_repository, "rev-parse", "HEAD")
    first = source_composite.export_client_release(
        app_ecosystem_repository=repository,
        app_ecosystem_ref=commit,
        kdcube_repository=kdcube_repository,
        kdcube_ref=first_kdcube_commit,
        root=tmp_path / "client-source",
    )
    owned = kdcube_repository / kdcube_paths[0] / "src" / "owned.py"
    owned.write_text("SOURCE = 'changed'\n", encoding="utf-8")
    _git(kdcube_repository, "add", ".")
    _git(kdcube_repository, "commit", "-qm", "change kdcube")
    second_kdcube_commit = _git(kdcube_repository, "rev-parse", "HEAD")

    second = source_composite.export_client_release(
        app_ecosystem_repository=repository,
        app_ecosystem_ref=commit,
        kdcube_repository=kdcube_repository,
        kdcube_ref=second_kdcube_commit,
        root=tmp_path / "client-source",
    )

    assert first.commit == second.commit == commit
    assert first.release_id != second.release_id


def test_code_bootstrap_accepts_only_declared_release_sources(tmp_path: Path) -> None:
    release = tmp_path / "release"
    paths = ["packages/a", "products/b"]
    for path in paths:
        (release / path / "src").mkdir(parents=True)

    roots = code_entrypoint._source_roots(release, {"source_paths": paths})

    assert roots == [str(release / path / "src") for path in paths]


def test_code_source_contract_pins_every_runtime_dependency_together() -> None:
    paths = relay_source.CLIENT_SOURCE_PATHS

    assert "products/project-board/packages/project-board" in paths
    assert "products/connection-hub/packages/connection-hub" in paths
    assert "products/connection-hub/packages/connection-hub-cli" in paths
    assert "app/ai-app/src/kdcube-ai-app/kdcube_cli" in paths


def test_client_source_never_inherits_the_old_relay_only_store(tmp_path: Path) -> None:
    config = tmp_path / "target" / "relay.json"
    legacy = config.parent / "relay-source"
    legacy.mkdir(parents=True)
    (legacy / "selection.json").write_text("{}\n", encoding="utf-8")

    root = relay_source.client_source_root(config)

    assert root == config.parent / "client-source"
    assert relay_source.read_selection(root) == {}


def test_installed_targets_keep_receipts_but_share_the_host_release_store(
    tmp_path: Path,
) -> None:
    target_root = (
        tmp_path
        / ".kdcube"
        / "client-runtime"
        / "problem-board"
        / "targets"
        / "dev"
        / "tenant__project"
        / "apps"
        / "problem-board@1-0"
        / "hosts"
    )
    config = target_root / "host-one" / "relay.json"
    other_config = target_root / "host-two" / "relay.json"
    config.parent.mkdir(parents=True)
    other_config.parent.mkdir(parents=True)
    config.write_text("{}\n", encoding="utf-8")
    other_config.write_text("{}\n", encoding="utf-8")

    assert relay_source.client_source_root(config) == config.parent / "client-source"
    assert relay_source.client_release_root(config) == (
        tmp_path
        / ".kdcube"
        / "client-runtime"
        / "tools"
        / "problem-board"
    )
    assert relay_source.host_target_config_paths(config) == (config, other_config)


def test_legacy_single_repository_selection_remains_readable(tmp_path: Path) -> None:
    repository, entrypoint_path, paths = _source_repository(tmp_path)
    commit = _git(repository, "rev-parse", "HEAD")
    root = tmp_path / "client-source"
    release = relay_source.export_release(
        repository=repository,
        ref=commit,
        root=root,
        entrypoint_path=entrypoint_path,
        source_paths=paths,
    )
    selection = relay_source.write_selection(
        root,
        {
            "mode": "snapshot",
            "commit": commit,
            "subtrees": release.subtrees,
        },
    )

    assert selection["schema"] == relay_source.LEGACY_SELECTION_SCHEMA
    assert relay_source.selected_release(root, selection).commit == commit


def test_snapshot_selection_requires_every_client_package_tree(tmp_path: Path) -> None:
    root = tmp_path / "client-source"

    with pytest.raises(DomainError) as refusal:
        relay_source.write_selection(
            root,
            {
                "mode": "snapshot",
                "commit": "a" * 40,
                "subtrees": {
                    "products/project-board/packages/project-board": "b" * 40,
                },
            },
        )

    assert refusal.value.code == "work_client_source_selection_invalid"


def test_composite_selection_rejects_a_missing_component(tmp_path: Path) -> None:
    root = tmp_path / "client-source"
    component = {
        "name": "app_ecosystem",
        "commit": "a" * 40,
        "subtrees": {path: "b" * 40 for path in APP_ECOSYSTEM_SOURCE_PATHS},
    }

    with pytest.raises(DomainError) as refusal:
        relay_source.write_selection(
            root,
            {
                "mode": "snapshot",
                "release_id": "c" * 64,
                "components": [component],
            },
        )

    assert refusal.value.code == "work_client_source_selection_invalid"


def test_composite_selection_rejects_compatibility_field_drift(
    tmp_path: Path,
) -> None:
    repository, _entrypoint, _paths = _source_repository(tmp_path)
    kdcube_repository, _kdcube_paths = _kdcube_repository(tmp_path)
    release = source_composite.export_client_release(
        app_ecosystem_repository=repository,
        app_ecosystem_ref="HEAD",
        kdcube_repository=kdcube_repository,
        kdcube_ref="HEAD",
        root=tmp_path / "client-source",
    )
    selection = relay_source.snapshot_selection(release)
    selection["commit"] = "f" * 40

    with pytest.raises(DomainError) as refusal:
        relay_source.write_selection(tmp_path / "selection", selection)

    assert refusal.value.code == "work_client_source_selection_invalid"


def test_use_code_cli_requires_and_names_both_repositories() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "source",
            "use-code",
            "--app-ecosystem-repository",
            "/app",
            "--app-ecosystem-ref",
            "app-ref",
            "--expect-app-ecosystem",
            "a" * 40,
            "--kdcube-repository",
            "/kdcube",
            "--kdcube-ref",
            "kdcube-ref",
            "--expect-kdcube",
            "b" * 40,
        ]
    )

    assert args.repository == "/app"
    assert args.ref == "app-ref"
    assert args.expect == "a" * 40
    assert args.kdcube_repository == "/kdcube"
    assert args.kdcube_ref == "kdcube-ref"
    assert args.expect_kdcube == "b" * 40

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "source",
                "use-code",
                "--repository",
                "/app",
                "--ref",
                "app-ref",
                "--expect",
                "a" * 40,
            ]
        )


def test_one_selection_drives_released_and_code_entrypoints(tmp_path: Path) -> None:
    repository, _code_entrypoint_path, _paths = _source_repository(tmp_path)
    commit = _git(repository, "rev-parse", "HEAD")
    kdcube_repository, _kdcube_paths = _kdcube_repository(tmp_path)
    kdcube_commit = _git(kdcube_repository, "rev-parse", "HEAD")
    config = tmp_path / "target" / "relay.json"
    root = relay_source.client_source_root(config)
    release = source_composite.export_client_release(
        app_ecosystem_repository=repository,
        app_ecosystem_ref=commit,
        kdcube_repository=kdcube_repository,
        kdcube_ref=kdcube_commit,
        root=root,
    )
    relay_source.activate_release(root, release)
    relay_source.write_selection(root, relay_source.snapshot_selection(release))

    command = entrypoint._selected_command(
        ["status", "--config", str(config)],
        current_source={"mode": "released", "version": "2026.09.22.2200"},
        config_path=config,
    )

    assert command is not None
    assert command[1] == str(release.script)
    assert command[2:] == ("status", "--config", str(config))
    assert entrypoint._selected_command(
        ["status"],
        current_source={"mode": "checkout", "head": commit, "dirty": False},
        config_path=config,
    ) is None

    relay_source.write_selection(
        root, relay_source.released_selection("2026.09.22.2200")
    )
    assert entrypoint._selected_command(
        ["status"],
        current_source={"mode": "released", "version": "2026.09.22.2200"},
        config_path=config,
    ) is None


def test_changed_installed_release_requires_explicit_selection(tmp_path: Path) -> None:
    config = tmp_path / "target" / "relay.json"
    relay_source.write_selection(
        relay_source.client_source_root(config),
        relay_source.released_selection("2026.09.22.2200"),
    )

    with pytest.raises(DomainError) as refusal:
        entrypoint._selected_command(
            ["status"],
            current_source={"mode": "released", "version": "2026.09.22.2201"},
            config_path=config,
        )

    assert refusal.value.code == "work_client_release_selection_mismatch"
    # Recovery source commands stay in the released bootstrap even when the
    # recorded version differs or the selected snapshot is broken.
    assert entrypoint._selected_command(
        ["source", "use-release", "--expect-version", "2026.09.22.2201"],
        current_source={"mode": "released", "version": "2026.09.22.2201"},
        config_path=config,
    ) is None


def test_a_release_environment_does_not_dispatch_through_a_target_receipt(
    tmp_path: Path,
) -> None:
    config = tmp_path / "target" / "relay.json"
    relay_source.write_selection(
        relay_source.client_source_root(config),
        relay_source.released_selection("2026.9.22.2200"),
    )

    assert entrypoint._selected_command(
        ["status", "--config", str(config)],
        current_source={
            "mode": "released",
            "version": "2026.9.22.2201",
            "release_id": "a" * 64,
        },
        config_path=config,
    ) is None


def test_released_selection_accepts_pep440_equivalent_version_spelling(
    tmp_path: Path,
) -> None:
    config = tmp_path / "target" / "relay.json"
    controller = ClientSourceController(
        config,
        service=_Service(tmp_path),
        release_source={"mode": "released", "version": "2026.9.22.2241"},
    )

    receipt = controller.use_release(
        expect_version="2026.09.22.2241", wait_seconds=0
    )

    assert receipt["selected"]["version"] == "2026.9.22.2241"
    assert receipt["installation"]["environment"]["imports"] == [
        "project_board.client.release_smoke"
    ]
    assert controller.launcher.is_file()
    assert str(release_install.active_pb(controller.root)) in controller.launcher.read_text(
        encoding="utf-8"
    )


def test_source_status_names_the_active_environment_and_launcher(
    tmp_path: Path,
) -> None:
    config = tmp_path / "target" / "relay.json"
    root = relay_source.client_source_root(config)
    release_id = "a" * 64
    selected = relay_source.released_selection("2026.9.22.2241")
    source_control.install_release_environment(
        root=root,
        release_id=release_id,
        requirements=("unused",),
        source=selected,
        base_python=Path("/usr/bin/python3"),
        expected_project_board_version="2026.9.22.2241",
    )
    release_install.activate_installed_release(root, release_id)
    relay_source.write_selection(root, selected)
    launcher = tmp_path / "bin" / "pb"
    launcher.parent.mkdir()
    launcher.write_text(
        "#!/bin/sh\n"
        f"# Project Board launcher version {release_install.LAUNCHER_VERSION}.\n"
        f"exec {release_install.active_pb(root)} \"$@\"\n",
        encoding="utf-8",
    )
    controller = ClientSourceController(
        config,
        service=_Service(tmp_path),
        release_source={"mode": "released", "version": "2026.9.22.2241"},
        launcher=launcher,
    )

    status = controller.status()

    assert status["active_release"]["release_id"] == release_id
    assert status["environment"]["python"].endswith(
        "/releases/current/venv/bin/python"
    )
    assert status["launcher"]["version"] == release_install.LAUNCHER_VERSION
    assert status["launcher"]["current"] is True


def test_activation_failure_preserves_relay_command_evidence(tmp_path: Path) -> None:
    class _FailsFirstRestart(_Service):
        def restart(self) -> dict[str, object]:
            self.restarts += 1
            if self.restarts == 1:
                raise DomainError(
                    "work_relay_service_command_failed",
                    "launchd could not restart the relay.",
                    details={
                        "command": ["launchctl", "bootstrap", "gui/501"],
                        "returncode": 5,
                        "stderr": "Bootstrap failed: 5: Input/output error",
                    },
                )
            return {"running": True}

    config = tmp_path / "target" / "relay.json"
    service = _FailsFirstRestart(tmp_path)
    controller = ClientSourceController(
        config,
        service=service,
        release_source={"mode": "released", "version": "2026.09.22.2241"},
    )

    with pytest.raises(DomainError) as failure:
        controller.use_release(
            expect_version="2026.09.22.2241", wait_seconds=0
        )

    assert failure.value.code == "work_client_source_activation_failed"
    assert failure.value.details["command"] == [
        "launchctl",
        "bootstrap",
        "gui/501",
    ]
    assert failure.value.details["returncode"] == 5
    assert failure.value.details["stderr"] == (
        "Bootstrap failed: 5: Input/output error"
    )
    assert failure.value.details["cause"]["code"] == (
        "work_relay_service_command_failed"
    )


def test_failed_environment_build_does_not_switch_or_restart_the_relay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "target" / "relay.json"
    root = relay_source.client_source_root(config)
    previous_id = "a" * 64
    previous_selection = relay_source.released_selection("2026.9.22.2200")
    source_control.install_release_environment(
        root=root,
        release_id=previous_id,
        requirements=("unused",),
        source=previous_selection,
        base_python=Path("/usr/bin/python3"),
    )
    release_install.activate_installed_release(root, previous_id)
    relay_source.write_selection(root, previous_selection)
    service = _Service(tmp_path)
    controller = ClientSourceController(
        config,
        service=service,
        release_source={"mode": "released", "version": "2026.9.22.2200"},
    )

    def fail_install(**_kwargs):
        raise release_install.ReleaseInstallError(
            "work_client_release_install_failed",
            "dependency resolution failed",
        )

    monkeypatch.setattr(source_control, "install_release_environment", fail_install)

    with pytest.raises(DomainError) as failure:
        controller.use_release(
            expect_version="2026.9.22.2201",
            wait_seconds=0,
        )

    assert failure.value.code == "work_client_release_install_failed"
    assert release_install.active_release_id(root) == previous_id
    assert relay_source.read_selection(root)["version"] == "2026.9.22.2200"
    assert service.restarts == 0


class _Service:
    def __init__(self, root: Path, *, startup_state: str = "started") -> None:
        self.definition_path = root / "service-definition"
        self.definition_path.parent.mkdir(parents=True, exist_ok=True)
        self.definition_path.write_text("bootstrap", encoding="utf-8")
        self.startup_state = startup_state
        self.running = True
        self.stops = 0
        self.restarts = 0

    def definition_uses_bootstrap(self) -> bool:
        return True

    def restart(self) -> dict[str, object]:
        self.restarts += 1
        self.running = True
        return {"running": True}

    def stop(self) -> dict[str, object]:
        self.stops += 1
        self.running = False
        return {"running": False}

    def await_source(
        self, expected: dict[str, object], *, since: str, wait_seconds: float
    ) -> dict[str, object]:
        del since, wait_seconds
        if self.startup_state == "started":
            return {"state": "started", "source": dict(expected)}
        # The rollback verification is allowed to succeed.
        self.startup_state = "started"
        return {"state": "source_mismatch", "observed": {"mode": "released"}}

    def status(self) -> dict[str, object]:
        return {"installed": True, "running": self.running}


def test_code_selection_is_verified_by_restarted_relay(tmp_path: Path) -> None:
    repository, _entrypoint_path, _paths = _source_repository(tmp_path)
    commit = _git(repository, "rev-parse", "HEAD")
    kdcube_repository, _kdcube_paths = _kdcube_repository(tmp_path)
    kdcube_commit = _git(kdcube_repository, "rev-parse", "HEAD")
    config = tmp_path / "target" / "relay.json"
    service = _Service(tmp_path)
    controller = ClientSourceController(
        config,
        service=service,
        release_source={"mode": "released", "version": "2026.09.22.2200"},
    )

    receipt = controller.use_code(
        repository=repository,
        ref="HEAD",
        expect=commit,
        kdcube_repository=kdcube_repository,
        kdcube_ref="HEAD",
        expect_kdcube=kdcube_commit,
        wait_seconds=0,
    )

    assert receipt["state"] == "activated"
    assert receipt["selected"]["commit"] == commit
    assert service.stops == 1
    assert service.restarts == 1
    assert receipt["installation"]["environment"]["imports"] == list(
        (*CLIENT_SOURCE_IMPORTS, *release_install.DEFAULT_IMPORT_SMOKE)
    )
    selected = relay_source.read_selection(controller.root)
    assert selected["mode"] == "snapshot" and selected["commit"] == commit
    assert selected["release_id"] == receipt["selected"]["release_id"]
    assert {
        path
        for component in selected["components"]
        for path in component["subtrees"]
    } == set(relay_source.CLIENT_SOURCE_PATHS)
    assert relay_source.source_line(selected).startswith(
        f"source=snapshot release={selected['release_id']}"
    )


def test_code_selection_refuses_an_unapproved_kdcube_commit(tmp_path: Path) -> None:
    repository, _entrypoint_path, _paths = _source_repository(tmp_path)
    commit = _git(repository, "rev-parse", "HEAD")
    kdcube_repository, _kdcube_paths = _kdcube_repository(tmp_path)
    config = tmp_path / "target" / "relay.json"
    controller = ClientSourceController(
        config,
        service=_Service(tmp_path),
        release_source={"mode": "released", "version": "2026.09.22.2200"},
    )

    with pytest.raises(DomainError) as refusal:
        controller.use_code(
            repository=repository,
            ref="HEAD",
            expect=commit,
            kdcube_repository=kdcube_repository,
            kdcube_ref="HEAD",
            expect_kdcube="f" * 40,
            wait_seconds=0,
        )

    assert refusal.value.code == "work_relay_source_commit_unexpected"
    assert relay_source.read_selection(controller.root) == {}


def test_code_selection_exports_the_commit_when_worktree_packages_are_deleted(
    tmp_path: Path,
) -> None:
    repository, _entrypoint_path, _paths = _source_repository(tmp_path)
    commit = _git(repository, "rev-parse", "HEAD")
    kdcube_repository, _kdcube_paths = _kdcube_repository(tmp_path)
    kdcube_commit = _git(kdcube_repository, "rev-parse", "HEAD")
    shutil.rmtree(repository / "packages")
    shutil.rmtree(repository / "products")
    config = tmp_path / "target" / "relay.json"
    service = _Service(tmp_path)
    controller = ClientSourceController(
        config,
        service=service,
        release_source={"mode": "released", "version": "2026.09.22.2200"},
    )

    receipt = controller.use_code(
        repository=repository,
        ref=commit,
        expect=commit,
        kdcube_repository=kdcube_repository,
        kdcube_ref=kdcube_commit,
        expect_kdcube=kdcube_commit,
        wait_seconds=0,
    )

    assert receipt["selected"]["commit"] == commit
    assert receipt["checkout"]["dirty"] is True


def test_failed_code_start_restores_released_selection(tmp_path: Path) -> None:
    repository, _entrypoint_path, _paths = _source_repository(tmp_path)
    commit = _git(repository, "rev-parse", "HEAD")
    kdcube_repository, _kdcube_paths = _kdcube_repository(tmp_path)
    kdcube_commit = _git(kdcube_repository, "rev-parse", "HEAD")
    config = tmp_path / "target" / "relay.json"
    service = _Service(tmp_path, startup_state="source_mismatch")
    controller = ClientSourceController(
        config,
        service=service,
        release_source={"mode": "released", "version": "2026.09.22.2200"},
    )

    with pytest.raises(DomainError) as failure:
        controller.use_code(
            repository=repository,
            ref="HEAD",
            expect=commit,
            kdcube_repository=kdcube_repository,
            kdcube_ref="HEAD",
            expect_kdcube=kdcube_commit,
            wait_seconds=0,
        )

    assert failure.value.code == "work_client_source_activation_failed"
    assert service.restarts == 2
    selected = relay_source.read_selection(controller.root)
    assert selected == {}
    assert release_install.active_release_id(controller.root) == ""
    assert not controller.launcher.exists()


def test_failed_rollback_is_reported_as_failed(tmp_path: Path) -> None:
    class _NeverStarts(_Service):
        def await_source(
            self, expected: dict[str, object], *, since: str, wait_seconds: float
        ) -> dict[str, object]:
            del expected, since, wait_seconds
            return {"state": "source_mismatch", "observed": {"mode": "unknown"}}

    repository, _entrypoint_path, _paths = _source_repository(tmp_path)
    commit = _git(repository, "rev-parse", "HEAD")
    kdcube_repository, _kdcube_paths = _kdcube_repository(tmp_path)
    kdcube_commit = _git(kdcube_repository, "rev-parse", "HEAD")
    config = tmp_path / "target" / "relay.json"
    controller = ClientSourceController(
        config,
        service=_NeverStarts(tmp_path),
        release_source={"mode": "released", "version": "2026.09.22.2200"},
    )

    with pytest.raises(DomainError) as failure:
        controller.use_code(
            repository=repository,
            ref=commit,
            expect=commit,
            kdcube_repository=kdcube_repository,
            kdcube_ref=kdcube_commit,
            expect_kdcube=kdcube_commit,
            wait_seconds=0,
        )

    assert failure.value.details["rollback"]["state"] == "restore_failed"


def test_rollback_restart_attempts_every_relay_and_reports_each_failure(
    tmp_path: Path,
) -> None:
    class _RestartFails(_Service):
        def restart(self) -> dict[str, object]:
            self.restarts += 1
            raise DomainError(
                "work_relay_service_command_failed",
                "relay restart failed",
                details={"returncode": 5},
            )

    class _StartupMismatch(_Service):
        def await_source(
            self,
            expected: dict[str, object],
            *,
            since: str,
            wait_seconds: float,
        ) -> dict[str, object]:
            del expected, since, wait_seconds
            return {"state": "source_mismatch", "observed": {"mode": "unknown"}}

    first_config = tmp_path / "targets" / "one" / "relay.json"
    second_config = tmp_path / "targets" / "two" / "relay.json"
    third_config = tmp_path / "targets" / "three" / "relay.json"
    first = _RestartFails(tmp_path / "services" / "one")
    second = _StartupMismatch(tmp_path / "services" / "two")
    third = _Service(tmp_path / "services" / "three")
    controller = ClientSourceController(
        first_config,
        service=first,
        host_services={second_config: second, third_config: third},
        release_source={"mode": "released", "version": "2026.9.22.2200"},
    )

    rollback = controller._restart_without_restore(
        previous_host_source={"mode": "released", "version": "2026.9.22.2200"},
        installed_services=[
            (first_config, first),
            (second_config, second),
            (third_config, third),
        ],
        wait_seconds=0,
    )

    assert rollback["state"] == "restore_failed"
    assert rollback["reason"] == "previous_relays_could_not_be_restarted"
    assert [item["config"] for item in rollback["restart_failures"]] == [
        str(first_config),
        str(second_config),
    ]
    assert rollback["restart_failures"][0]["cause"]["code"] == (
        "work_relay_service_command_failed"
    )
    assert rollback["restart_failures"][1]["startup"]["state"] == (
        "source_mismatch"
    )
    assert [item["config"] for item in rollback["host_relays"]] == [
        str(third_config)
    ]
    assert first.restarts == second.restarts == third.restarts == 1


def test_host_switch_updates_and_restarts_every_target(tmp_path: Path) -> None:
    first_config = tmp_path / "targets" / "one" / "relay.json"
    second_config = tmp_path / "targets" / "two" / "relay.json"
    first = _Service(tmp_path / "services" / "one")
    second = _Service(tmp_path / "services" / "two")
    controller = ClientSourceController(
        first_config,
        service=first,
        host_services={second_config: second},
        release_source={"mode": "released", "version": "2026.9.22.2200"},
    )

    receipt = controller.use_release(
        expect_version="2026.9.22.2201",
        wait_seconds=0,
    )

    assert receipt["state"] == "activated"
    assert first.stops == second.stops == 1
    assert first.restarts == second.restarts == 1
    assert {item["config"] for item in receipt["host_relays"]} == {
        str(first_config.resolve()),
        str(second_config.resolve()),
    }
    for config in (first_config, second_config):
        assert relay_source.read_selection(
            relay_source.client_source_root(config)
        )["version"] == "2026.9.22.2201"


def test_relay_stop_failure_does_not_move_the_host_release(tmp_path: Path) -> None:
    class _StopFails(_Service):
        def stop(self) -> dict[str, object]:
            self.stops += 1
            return {"running": True}

    config = tmp_path / "target" / "relay.json"
    service = _StopFails(tmp_path / "service")
    controller = ClientSourceController(
        config,
        service=service,
        release_source={"mode": "released", "version": "2026.9.22.2200"},
    )

    with pytest.raises(DomainError) as failure:
        controller.use_release(
            expect_version="2026.9.22.2201",
            wait_seconds=0,
        )

    assert failure.value.code == "work_client_source_activation_failed"
    assert failure.value.details["rollback"]["state"] == "restored"
    assert release_install.active_release_id(controller.root) == ""
    assert relay_source.read_selection(controller.selection_root) == {}
    assert not controller.launcher.exists()
    assert service.stops == 1
    assert service.restarts == 1


def test_host_switch_failure_restores_release_receipts_and_launcher(
    tmp_path: Path,
) -> None:
    first_config = tmp_path / "targets" / "one" / "relay.json"
    second_config = tmp_path / "targets" / "two" / "relay.json"
    first = _Service(tmp_path / "services" / "one")
    second = _Service(
        tmp_path / "services" / "two", startup_state="source_mismatch"
    )
    root = relay_source.client_release_root(first_config)
    previous_id = release_install.published_release_id("2026.9.22.2200")
    previous_selection = relay_source.released_selection(
        "2026.9.22.2200", selected_at="2026-09-25T00:00:00Z"
    )
    source_control.install_release_environment(
        root=root,
        release_id=previous_id,
        requirements=("unused",),
        source=previous_selection,
        base_python=Path("/usr/bin/python3"),
    )
    release_install.activate_installed_release(root, previous_id)
    prior_receipts: dict[Path, dict[str, object]] = {}
    for config in (first_config, second_config):
        prior_receipts[config] = relay_source.write_selection(
            relay_source.client_source_root(config), previous_selection
        )

    launcher = tmp_path / "bin" / "pb"
    launcher.parent.mkdir(parents=True)
    launcher.write_text(
        "#!/bin/sh\n"
        f"# {release_install.LAUNCHER_MARKER}\n"
        "# prior launcher content\n"
        f'exec {release_install.active_pb(root)} "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o700)
    prior_launcher = launcher.read_bytes()

    controller = ClientSourceController(
        first_config,
        service=first,
        host_services={second_config: second},
        release_source=previous_selection,
        launcher=launcher,
    )

    with pytest.raises(DomainError) as failure:
        controller.use_release(
            expect_version="2026.9.22.2201",
            wait_seconds=0,
        )

    assert failure.value.code == "work_client_source_activation_failed"
    assert failure.value.details["rollback"]["state"] == "restored"
    assert release_install.active_release_id(root) == previous_id
    assert launcher.read_bytes() == prior_launcher
    assert launcher.stat().st_mode & 0o777 == 0o700
    assert first.stops == second.stops == 2
    assert first.restarts == second.restarts == 2
    for config, previous in prior_receipts.items():
        assert relay_source.read_selection(
            relay_source.client_source_root(config)
        ) == previous
