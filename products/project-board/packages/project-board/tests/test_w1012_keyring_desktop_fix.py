"""pb status gives a desktop fix only when an active graphical session is proven (W1012).

On a desktop the server unlock line's pkill and --replace stop the session's
own keyring, and the worker skill hands out the fix pb status prints. The
design review (codex-infra, 7 Nov 2026): the desktop text only when loginctl
proves an active x11 or wayland session of this UID; otherwise both cases are
named; loginctl calls bounded; a probe failure never yields the server line
alone.
"""

from __future__ import annotations

import subprocess

import pytest

from project_board.client import prerequisites
from project_board.client.prerequisites import (
    KEYRING_DESKTOP_FIX,
    KEYRING_EITHER_FIX,
    KEYRING_UNLOCK_LINE,
    Probes,
    check_prerequisites,
    graphical_session_active,
)


def _done(stdout, code=0):
    return subprocess.CompletedProcess([], code, stdout=stdout, stderr="")


def _probes(sessions, *, keyring=(False, "the login keyring is locked"), uid=1000, calls=None):
    """sessions: {id: "Type=..\\nActive=..\\nUser=.."}; None makes show-user fail."""

    def run(command):
        if calls is not None:
            calls.append(list(command))
        if command[:2] == ["loginctl", "show-user"]:
            if sessions is None:
                return None
            return _done(" ".join(sessions))
        if command[:2] == ["loginctl", "show-session"]:
            shown = (sessions or {}).get(command[2])
            return _done(shown) if shown is not None else _done("", code=1)
        if command[:3] == ["systemctl", "--user", "show-environment"]:
            return _done("")
        if command[:2] == ["loginctl", "show-user"] or command[:1] == ["loginctl"]:
            return _done("Linger=yes")
        return _done("")

    return Probes(
        system=lambda: "Linux",
        which=lambda _name: "/usr/bin/x",
        run=run,
        user=lambda: "worker",
        os_release=lambda: "ID=ubuntu\n",
        keyring_state=lambda: keyring,
        uid=lambda: uid,
    )


def _keyring_fix(probes):
    items = {item["name"]: item for item in check_prerequisites(probes)["checked"]}
    return items["keyring"]["fix"]


@pytest.mark.parametrize("kind", ["x11", "wayland"])
def test_an_active_graphical_session_of_this_uid_gets_the_desktop_fix(kind):
    probes = _probes({"2": f"Type={kind}\nActive=yes\nUser=1000\n"})
    assert graphical_session_active(probes)
    fix = _keyring_fix(probes)
    assert fix == KEYRING_DESKTOP_FIX
    assert "do not run an unlock line" in fix and "pkill" not in fix and "--replace" not in fix
    assert "standard login keyring" in fix and "Passwords and Keys" in fix


@pytest.mark.parametrize(
    "sessions",
    [
        None,  # loginctl missing, failing or timed out (the runner returns None)
        {},  # no session at all
        {"5": "Type=tty\nActive=yes\nUser=1000\n"},  # an ssh or console session
        {"2": "Type=x11\nActive=no\nUser=1000\n"},  # a desktop session that is not active
        {"2": "Type=wayland\nActive=yes\nUser=1001\n"},  # someone else's desktop
        {"2": "garbage"},  # unreadable output
    ],
)
def test_anything_short_of_proof_names_both_cases_never_the_server_line_alone(sessions):
    probes = _probes(sessions)
    assert not graphical_session_active(probes)
    fix = _keyring_fix(probes)
    assert fix == KEYRING_EITHER_FIX
    assert fix != KEYRING_UNLOCK_LINE
    assert fix.index("do not run an unlock line") < fix.index("Only on a machine you reach over ssh")
    assert fix.endswith(KEYRING_UNLOCK_LINE)


def test_loginctl_reads_are_bounded_in_sessions_and_use_the_uid_not_user():
    calls = []
    sessions = {str(index): "Type=tty\nActive=yes\nUser=1000\n" for index in range(40)}
    probes = _probes(sessions, calls=calls, uid=1000)
    assert not graphical_session_active(probes)
    shown = [call for call in calls if call[:2] == ["loginctl", "show-session"]]
    assert len(shown) == prerequisites.LOGINCTL_SESSION_LIMIT
    assert ["loginctl", "show-user", "1000", "-p", "Sessions", "--value"] in calls
    assert not any("worker" in call for call in calls if call[:1] == ["loginctl"] and call[1] != "enable-linger")


def test_a_usable_store_runs_no_session_probe():
    calls = []
    probes = _probes({"2": "Type=x11\nActive=yes\nUser=1000\n"}, keyring=(True, "secretstorage"), calls=calls)
    check_prerequisites(probes)
    assert not any(call[:2] == ["loginctl", "show-session"] for call in calls)


def test_an_unreadable_uid_is_not_proof():
    def broken():
        raise OSError("no uid")

    probes = _probes({"2": "Type=x11\nActive=yes\nUser=1000\n"}, uid=broken)
    probes = Probes(**{**probes.__dict__, "uid": broken})
    assert not graphical_session_active(probes)
