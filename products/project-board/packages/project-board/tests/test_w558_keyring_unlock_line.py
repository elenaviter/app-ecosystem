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
