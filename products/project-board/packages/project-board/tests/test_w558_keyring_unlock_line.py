"""W558: the keyring unlock line works where a systemd user unit owns the Secret Service.

On host mint (Linux Mint 22.3, gnome-keyring 46.1, ssh only), 2026-10-05, the
unit's daemon kept `org.freedesktop.secrets` on a locked store and the former
line started a second daemon the board never talked to. Stopping the unit, its
socket and leftover daemons first, then the same unlock, worked (note_2113f9a5).
The operator also read the silent `read -rs` as "nothing asked me for password".
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from project_board.client.prerequisites import KEYRING_UNLOCK_LINE

ROOT = Path(__file__).resolve().parents[1]
PROCEDURE = ROOT / "src" / "project_board" / "procedures" / "add-a-worker-host.md"
PUBLIC = ROOT.parents[1] / "docs" / "add-a-machine.md"


def test_the_line_stops_the_unit_and_leftover_daemons_before_unlocking():
    stop = KEYRING_UNLOCK_LINE.index("systemctl --user stop gnome-keyring-daemon.socket gnome-keyring-daemon.service")
    kill = KEYRING_UNLOCK_LINE.index('pkill -u "$USER" -x gnome-keyring-d')
    unlock = KEYRING_UNLOCK_LINE.index("gnome-keyring-daemon --replace --unlock --components=secrets")
    assert stop < kill < unlock


def test_the_line_says_it_is_waiting_for_the_password():
    assert "printf 'Password store password (not shown): '" in KEYRING_UNLOCK_LINE
    assert KEYRING_UNLOCK_LINE.index("(not shown)") < KEYRING_UNLOCK_LINE.index("read -rs P")
    assert KEYRING_UNLOCK_LINE.endswith("unset P")


def test_the_procedure_and_the_public_page_print_exactly_this_line_with_a_check():
    for page in (PROCEDURE, PUBLIC):
        text = page.read_text(encoding="utf-8")
        assert KEYRING_UNLOCK_LINE in text, page
        assert "busctl --user status org.freedesktop.secrets" in text, page
        assert "printf 'Again: '" in text, page  # a new password is typed twice


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_the_line_parses_in_the_usual_shells(shell):
    if not shutil.which(shell):
        pytest.skip(f"{shell} is not installed")
    assert subprocess.run([shell, "-n", "-c", KEYRING_UNLOCK_LINE], check=False).returncode == 0


def test_the_procedure_forbids_an_improvised_keyring_command():
    text = PROCEDURE.read_text(encoding="utf-8")
    assert "**Only these lines.**" in text and "never another keyring command of its own" in text
    assert "If the check fails twice, stop, change nothing more" in text


# W557, folded into W558 (operator, 2026-10-05: "why you multiply these issues ?"):
# `pb status` never unlocks or creates a store, and gives up instead of hanging.

import sys
import threading
import types

from project_board.client import prerequisites
from procedure_reference import reference_text


def _fake_secret_service(monkeypatch, *, locked=False, missing=False, block=None):
    calls = {"unlock": 0, "create": 0}

    class ItemNotFoundException(Exception):
        pass

    class Collection:
        def __init__(self, connection):
            if block is not None:
                block.wait()
            if missing:
                raise ItemNotFoundException()

        def is_locked(self):
            return locked

        def unlock(self):
            calls["unlock"] += 1

    secretstorage = types.ModuleType("secretstorage")
    secretstorage.dbus_init = lambda: object()

    def get_default_collection(connection):
        calls["create"] += 1

    secretstorage.get_default_collection = get_default_collection
    collection_mod = types.ModuleType("secretstorage.collection")
    collection_mod.Collection = Collection
    exceptions_mod = types.ModuleType("secretstorage.exceptions")
    exceptions_mod.ItemNotFoundException = ItemNotFoundException
    monkeypatch.setitem(sys.modules, "secretstorage", secretstorage)
    monkeypatch.setitem(sys.modules, "secretstorage.collection", collection_mod)
    monkeypatch.setitem(sys.modules, "secretstorage.exceptions", exceptions_mod)

    class SecretServiceKeyring:
        def get_preferred_collection(self):
            calls["unlock"] += 1  # keyring's own path would unlock here

    keyring = types.ModuleType("keyring")
    keyring.get_keyring = lambda: SecretServiceKeyring()
    monkeypatch.setitem(sys.modules, "keyring", keyring)
    return calls


def test_a_locked_store_is_reported_without_unlocking(monkeypatch):
    calls = _fake_secret_service(monkeypatch, locked=True)

    assert prerequisites._keyring_state(timeout=2) == (False, "the login keyring is locked")
    assert calls == {"unlock": 0, "create": 0}


def test_a_missing_store_is_reported_without_creating_one(monkeypatch):
    calls = _fake_secret_service(monkeypatch, missing=True)

    usable, detail = prerequisites._keyring_state(timeout=2)
    assert (usable, detail) == (False, "the Secret Service has no default store")
    assert calls["create"] == 0
    item = prerequisites._keyring(prerequisites.Probes(keyring_state=lambda: (usable, detail)), "debian", mac=False)
    assert item["fix"] == KEYRING_UNLOCK_LINE


def test_a_store_that_never_answers_ends_in_time_instead_of_hanging(monkeypatch):
    never = threading.Event()
    _fake_secret_service(monkeypatch, block=never)

    usable, detail = prerequisites._keyring_state(timeout=0.3)

    assert usable is False and "does not answer in time" in detail
    never.set()


def test_the_reset_compares_both_entries_before_it_moves_or_stops_anything():
    """Review of #526 (claude-main): the reset set the store aside, then compared, and on a
    mismatch said "nothing changed" with no store left."""

    for page in (PROCEDURE, PUBLIC):
        text = page.read_text(encoding="utf-8")
        line = next(line for line in text.splitlines() if "printf 'New password: '" in line)
        compare = line.index('[ "$P" = "$Q" ]; then')
        assert compare < line.index("mv ~/.local/share/keyrings/login.keyring")
        assert compare < line.index("systemctl --user stop gnome-keyring-daemon.socket")
        assert compare < line.index("gnome-keyring-daemon --replace --unlock")
        assert "mv ~/.local/share/keyrings/login.keyring" not in text.split(line)[0].split("**Reset**")[-1]


def test_a_mismatched_reset_leaves_the_store_in_place(tmp_path):
    if not shutil.which("bash"):
        pytest.skip("bash is not installed")
    line = next(
        line for line in PROCEDURE.read_text(encoding="utf-8").splitlines() if "printf 'New password: '" in line
    )
    keyrings = tmp_path / ".local" / "share" / "keyrings"
    keyrings.mkdir(parents=True)
    (keyrings / "login.keyring").write_text("store", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("systemctl", "pkill", "gnome-keyring-daemon"):
        stub = bin_dir / tool
        stub.write_text("#!/bin/sh\necho called >> \"$HOME/called\"\n", encoding="utf-8")
        stub.chmod(0o755)
    result = subprocess.run(
        ["bash", "-c", line], input="first\nsecond\n", capture_output=True, text=True,
        env={"HOME": str(tmp_path), "PATH": f"{bin_dir}:/usr/bin:/bin", "USER": "test"}, check=False,
    )
    assert "the two entries are empty or differ; nothing changed" in result.stdout
    assert (keyrings / "login.keyring").read_text(encoding="utf-8") == "store"
    assert not (tmp_path / "called").exists()

def test_the_new_machine_and_upgrade_procedures_carry_the_keyring_story():
    """Operator, 2026-10-05: the procedures must carry it "so the agents will guide the user
    properly instead of making up the non-existing things"."""

    procedures = ROOT / "src" / "project_board" / "procedures"
    first_run = (procedures / "problem-board-worker" / "references" / "first-run.md").read_text(encoding="utf-8")
    upgrade = (procedures / "install-update-rollback.md").read_text(encoding="utf-8")
    assert "**The keyring item on headless Linux (W558).**" in first_run
    assert "Never give another keyring command of your own" in first_run
    assert "with no restart" in first_run
    assert "credential_store_locked" in upgrade and "add-a-worker-host step 6" in upgrade
    assert "picks the unlock up on its next attempt" in PROCEDURE.read_text(encoding="utf-8")


def test_a_locked_store_is_a_transient_relay_failure():
    from connection_hub.caller.errors import AuthorizationError, CredentialError
    from project_board.client.relay import transient_failure

    assert transient_failure(CredentialError("credential_store_locked", "x"))
    assert transient_failure(CredentialError("credential_store_missing", "x"))
    assert transient_failure(AuthorizationError("oauth_credential_custody_timeout", "x"))
    # The same conditions read through the OAuth stores (mint, 2026-10-08).
    for code in ("oauth_profile_store_locked", "oauth_profile_store_missing",
                 "oauth_session_store_locked", "oauth_session_store_missing"):
        assert transient_failure(AuthorizationError(code, "x")), code
    # A real store fault still parks the channel.
    assert not transient_failure(AuthorizationError("oauth_profile_store_failed", "x"))


@pytest.mark.parametrize("code", ["oauth_profile_store_locked", "oauth_profile_store_missing"])
def test_a_channel_opened_while_the_oauth_store_is_locked_is_due_again_within_seconds(tmp_path, code, caplog):
    """Host mint, 2026-10-08: the relay started at 18:47:10Z with the store
    locked, recorded the open failure as a permanent refusal (next attempt
    about 30 minutes out), and the 19:01Z unlock was not picked up before the
    19:09Z restart. A locked or missing store is now due again in seconds."""

    import asyncio

    from connection_hub.caller.errors import AuthorizationError
    from project_board.client import relay_pacing
    from relay_helpers import make_host, make_supervisor

    def due_after(error, seconds):
        root = tmp_path / error.code
        root.mkdir()
        host, _identity, channel = make_host(root)
        supervisor = make_supervisor(host)
        now = [1_000_000.0]
        supervisor._pacing = relay_pacing.RelayPacing(None, clock=lambda: now[0], rng=lambda: 1.0)
        asyncio.run(supervisor._finish_pending_turn(host, channel, "profile-fingerprint", error))
        now[0] += seconds
        return supervisor._pacing.pending_due(channel.worker_name, "profile-fingerprint")

    assert due_after(AuthorizationError(code, "locked"), 60)
    # The generic store fault keeps the permanent refusal's long wait.
    assert not due_after(AuthorizationError("oauth_profile_store_failed", "fault"), 60)



def test_an_empty_password_changes_nothing(tmp_path):
    """Review of #526 (claude-main, P3): two empty entries matched and created an empty-password store."""

    if not shutil.which("bash"):
        pytest.skip("bash is not installed")
    reset = next(line for line in PROCEDURE.read_text(encoding="utf-8").splitlines() if "printf 'New password: '" in line)
    for line, typed in ((reset, "\n\n"), (KEYRING_UNLOCK_LINE, "\n")):
        home = tmp_path / str(abs(hash(line)))
        keyrings = home / ".local" / "share" / "keyrings"
        keyrings.mkdir(parents=True)
        (keyrings / "login.keyring").write_text("store", encoding="utf-8")
        bin_dir = home / "bin"
        bin_dir.mkdir()
        for tool in ("systemctl", "pkill", "gnome-keyring-daemon"):
            stub = bin_dir / tool
            stub.write_text("#!/bin/sh\necho called >> \"$HOME/called\"\n", encoding="utf-8")
            stub.chmod(0o755)
        result = subprocess.run(
            ["bash", "-c", line], input=typed, capture_output=True, text=True,
            env={"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin", "USER": "test"}, check=False,
        )
        assert "nothing changed" in result.stdout
        assert (keyrings / "login.keyring").read_text(encoding="utf-8") == "store"
        assert not (home / "called").exists()


def test_every_start_or_resume_begins_with_the_machine_self_test():
    """Operator, 2026-10-05 (W258 decision a): "agents when i resume them can check that and
    say to a usr what he should do in order to restore the servuce (unlock keyring)"."""

    skill = reference_text(ROOT / "src" / "project_board" / "procedures" / "problem-board-worker" / "SKILL.md")
    section = skill[skill.index("## Start Or Resume"):]
    test_at = section.index("**First, the machine self-test.**")
    assert test_at < section.index("1. Read the repository instructions")
    paragraph = section[test_at:section.index("1. Read the repository instructions")]
    assert "begins with `pb status`" in paragraph and "a session resumed after its machine restarted" in paragraph
    assert "add-a-worker-host step 6" in paragraph and "no keyring command of your own" in paragraph
