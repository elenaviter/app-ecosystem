from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _no_host_output_format(monkeypatch):
    """The host's PB_FORMAT never reaches a test (W349).

    The worker procedure tells every agent to `export PB_FORMAT=brief`, so a
    suite started from such a shell ran `pb` (in-process, or as a child that
    inherits the environment) in brief mode, and a test reading its JSON
    failed only in those runs.
    """

    monkeypatch.delenv("PB_FORMAT", raising=False)


@pytest.fixture(autouse=True)
def _no_host_git_config(monkeypatch):
    """The host's global and system Git configuration never reaches a test.

    Tests create throwaway repositories and commit in them. A machine's own
    global Git setup (for example an identity guard in a global hooks
    directory, or ``user.useConfigOnly``) then judged those test commits and
    failed tests whose code was correct: dev-main, 2026-10-12, after its
    GitHub identity setup. Git reads ``GIT_CONFIG_GLOBAL`` instead of
    ``~/.gitconfig`` and skips ``/etc/gitconfig`` with ``GIT_CONFIG_NOSYSTEM``;
    child processes inherit both. A test that needs its own global file still
    sets it with ``monkeypatch.setenv``, which wins over this default.
    """

    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture(autouse=True)
def _no_host_machine_configuration(monkeypatch, tmp_path_factory):
    """The machine's own Problem Board configuration never reaches a test.

    `resolve_host_config_path` falls back to `PROBLEM_BOARD_CONFIG` and then to the
    default pointer under the client runtime root (`~/.kdcube/client-runtime`). On a
    machine where pb is set up, `pb procedure install` in a test then found the real
    host and tried to switch it to the package under test; in the pb-set release
    gate (project-board installed from its wheel, not a checkout) that refused with
    `work_client_install_not_a_commit` and three install tests failed only on such
    hosts. Each test starts with an empty client runtime root and no config override;
    a test that needs either sets it with ``monkeypatch.setenv``.
    """

    monkeypatch.delenv("PROBLEM_BOARD_CONFIG", raising=False)
    monkeypatch.setenv("KDCUBE_CLIENT_RUNTIME_HOME", str(tmp_path_factory.mktemp("client-runtime")))


@pytest.fixture(autouse=True)
def _no_session_claude_profile(monkeypatch):
    """A test never sees the Claude profile of the session that runs it (W763).

    `pb procedure install` and the status line read `CLAUDE_CONFIG_DIR` as this
    session's own profile; a suite run from a profile-bound session would
    otherwise install into, or attribute usage to, that real profile. A test
    that needs the variable sets it with ``monkeypatch.setenv``.
    """

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
