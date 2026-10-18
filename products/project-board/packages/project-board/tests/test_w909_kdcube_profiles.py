"""Two KDCube profiles on one host (W909).

Operator, 2026-10-18: "can we have on the same machine 2 relays working for 2
kdcubes?", then "1. Two relays at once: << but this we nee3d" and "3. Choosing
the KDCube true. must accept something like \"profile\"". A KDCube profile is
the target id `pb setup --target-id` names. Setting up a second one keeps the
machine default; every host command selects one with `--profile` or
`PB_PROFILE`; an unknown or ambiguous name is refused, never answered with the
default.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from project_board.client import cli, entrypoint, host_config
from project_board.client.relay_service import RelayService
from project_board.contract.errors import DomainError


def _setup(tmp_path: Path, target: str, *, project: str = "project", make_default: bool = False) -> Path:
    root = tmp_path / "roots" / target
    root.mkdir(parents=True, exist_ok=True)
    config = host_config.initialize_host_config(
        target_id=target,
        endpoint=f"https://{target}.example/mcp",
        tenant="tenant",
        platform_project=project,
        host_id="host-one",
        allowed_roots=[str(root)],
        source_repositories={},
        make_default=make_default,
    )
    return config.path


def _default() -> str:
    return str(host_config.read_json(host_config.default_pointer_path(), required=False).get("config") or "")


def test_the_first_setup_becomes_the_default(tmp_path):
    local = _setup(tmp_path, "local")
    assert _default() == str(local)


def test_a_second_profile_keeps_the_existing_default(tmp_path):
    local = _setup(tmp_path, "local")
    cloud = _setup(tmp_path, "cloud")
    assert _default() == str(local), "sessions without a profile must not move to the new KDCube"
    assert host_config.profile_config_path("cloud") == cloud


def test_make_default_moves_the_default(tmp_path):
    _setup(tmp_path, "local")
    cloud = _setup(tmp_path, "cloud", make_default=True)
    assert _default() == str(cloud)


def test_a_default_whose_config_is_gone_is_replaced(tmp_path):
    local = _setup(tmp_path, "local")
    local.unlink()
    cloud = _setup(tmp_path, "cloud")
    assert _default() == str(cloud)


def test_precedence_flag_then_env_then_default(tmp_path, monkeypatch):
    local = _setup(tmp_path, "local")
    cloud = _setup(tmp_path, "cloud")
    other = tmp_path / "other.json"

    assert host_config.host_config_selection() == (local, "default")
    monkeypatch.setenv("PB_PROFILE", "cloud")
    assert host_config.host_config_selection() == (cloud, "profile_env")
    monkeypatch.setenv("PROBLEM_BOARD_CONFIG", str(other))
    assert host_config.host_config_selection() == (other.resolve(), "config_env"), "an env path beats an env name"
    assert host_config.host_config_selection(profile="local") == (local, "profile_flag"), "a flag beats the env"
    assert host_config.host_config_selection(str(cloud), profile="local") == (cloud, "config_flag")


def test_an_unknown_profile_is_refused_never_the_default(tmp_path, monkeypatch):
    _setup(tmp_path, "local")
    with pytest.raises(DomainError) as refused:
        host_config.profile_config_path("nowhere")
    assert refused.value.code == "work_profile_unknown"
    assert refused.value.details["known_profiles"] == ["local"]
    monkeypatch.setenv("PB_PROFILE", "nowhere")
    with pytest.raises(DomainError) as refused:
        host_config.resolve_host_config_path()
    assert refused.value.code == "work_profile_unknown"


def test_one_name_for_two_deployment_projects_is_ambiguous(tmp_path):
    _setup(tmp_path, "cloud", project="one")
    _setup(tmp_path, "cloud", project="two")
    with pytest.raises(DomainError) as refused:
        host_config.profile_config_path("cloud")
    assert refused.value.code == "work_profile_ambiguous"
    assert len(refused.value.details["configs"]) == 2


def test_the_profile_flag_reaches_every_host_command(tmp_path):
    _setup(tmp_path, "local")
    cloud = _setup(tmp_path, "cloud")
    for argv in (
        ["worker", "watch", "--profile", "cloud"],
        ["worker", "receive", "--profile", "cloud"],
        ["relay-service", "install", "--profile", "cloud"],
        ["status", "--profile", "cloud"],
    ):
        args = cli.build_parser().parse_args(argv)
        cli._select_kdcube_profile(args)
        assert args.config == str(cloud), argv
    explicit = cli.build_parser().parse_args(["worker", "receive", "--config", "/x.json", "--profile", "cloud"])
    cli._select_kdcube_profile(explicit)
    assert explicit.config == "/x.json", "--config wins over --profile"


def test_status_names_the_profile_and_what_chose_it(tmp_path):
    _setup(tmp_path, "local")
    _setup(tmp_path, "cloud")
    args = cli.build_parser().parse_args(["status", "--profile", "cloud"])
    cli._select_kdcube_profile(args)
    view = cli._kdcube_profile_view(args)
    assert (view["name"], view["selected_by"], view["known"]) == ("cloud", "profile_flag", ["cloud", "local"])


def test_an_unknown_profile_flag_fails_the_command(tmp_path, capsys):
    _setup(tmp_path, "local")
    assert cli.main(["status", "--profile", "nowhere"]) == 1
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "work_profile_unknown"


def test_the_source_selection_scan_reads_the_profile(tmp_path):
    _setup(tmp_path, "local")
    cloud = _setup(tmp_path, "cloud")
    assert entrypoint._config_argument(["worker", "watch", "--profile", "cloud"]) == cloud
    assert entrypoint._config_argument(["worker", "watch", "--profile=cloud"]) == cloud
    assert entrypoint._config_argument(["worker", "watch"]) != cloud


def test_two_profiles_get_two_relay_services(tmp_path):
    local = RelayService.create(_setup(tmp_path, "local"), system="Darwin", home=tmp_path / "home")
    cloud = RelayService.create(_setup(tmp_path, "cloud"), system="Darwin", home=tmp_path / "home")
    assert local.service_id != cloud.service_id
    assert local.definition_path != cloud.definition_path
    assert "--config" in cloud.program_arguments and str(cloud.config_path) in cloud.program_arguments
