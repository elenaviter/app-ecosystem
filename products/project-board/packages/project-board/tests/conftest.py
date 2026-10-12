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
