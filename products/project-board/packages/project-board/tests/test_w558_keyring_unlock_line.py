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
