from __future__ import annotations

from connection_hub_cli import paths


def test_module_mode_uses_its_current_interpreter_instead_of_a_path_command(
    monkeypatch,
) -> None:
    monkeypatch.setattr(paths.sys, "executable", "/current/environment/bin/python")
    monkeypatch.setattr(
        paths.shutil,
        "which",
        lambda _name: "/different/environment/bin/connection-hub",
    )

    launch = paths.resolve_helper_launch("/source/connection_hub_cli/__main__.py")

    assert launch.command == "/current/environment/bin/python"
    assert launch.prefix_args == ("-m", "connection_hub_cli")
