"""A stranger's new machine is checked, and the skill finds its pb (W304 new-machine walk).

The operator's walk on a headless Linux machine (2026-09-27) found: nothing
checked the machine (tmux missing, linger off, keyring locked over ssh), the
person did not know which fixes need an administrator or that any
administrator account can run them, the agent did not find the pb installed
outside ~/.local, it asked for "the approved version", and the setup helper
and the worker session were never told apart.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from project_board.client.first_run import MACHINE_NOT_CONFIGURED, first_run_status
from project_board.client.prerequisites import ADMIN_NOTE, KEYRING_UNLOCK_LINE, Probes, check_prerequisites
from project_board.client.procedures import INSTALLED_BY, install_agent_procedure, source_package_path

MINT = 'NAME="Linux Mint"\nID=linuxmint\nID_LIKE="ubuntu debian"\n'


def _done(stdout: str = "", code: int = 0):
    return subprocess.CompletedProcess(["probe"], code, stdout, "")


def _linux(*, tools=("python3", "git", "tmux"), linger="yes", systemd=True, keyring=(True, "SecretService")):
    def run(command):
        if command[0].endswith("python3"):
            return _done("3.12\n")
        if command[0] == "loginctl":
            return _done(f"{linger}\n") if linger is not None else None
        if command[0] == "systemctl":
            return _done() if systemd else _done(code=1)
        return None

    return Probes(
        system=lambda: "Linux",
        which=lambda name: f"/usr/bin/{name}" if name in tools else None,
        run=run,
        user=lambda: "elena",
        os_release=lambda: MINT,
        keyring_state=lambda: keyring,
    )


def _by_name(result):
    return {item["name"]: item for item in result["checked"]}


def test_a_ready_linux_host_has_nothing_missing():
    result = check_prerequisites(_linux())
    assert result["os"] == "linux" and result["ok"] is True and result["missing"] == []
    assert set(_by_name(result)) == {"python", "git", "tmux", "linger", "systemd user manager", "keyring"}


def test_the_mint_walk_names_each_missing_one_with_its_fix_and_who_can_run_it():
    result = check_prerequisites(_linux(tools=("python3", "git"), linger="no", keyring=(False, "the login keyring is locked")))
    assert result["missing"] == ["tmux", "linger", "keyring"]
    assert result["needs_admin"] == ["tmux", "linger"]
    items = _by_name(result)
    assert items["tmux"]["fix"] == "sudo apt install -y tmux"
    assert items["tmux"]["admin_note"] == ADMIN_NOTE and "Any administrator account" in ADMIN_NOTE
    assert "terminal stays open" in items["tmux"]["without_it"]
    assert items["linger"]["fix"] == "sudo loginctl enable-linger elena"
    assert "relay stops" in items["linger"]["without_it"]
    # A locked keyring over ssh is unlocked by the person, no admin (W258).
    assert items["keyring"]["fix"] == KEYRING_UNLOCK_LINE and items["keyring"]["needs_admin"] is False


def test_a_fedora_host_gets_dnf_lines_and_a_mac_its_own_set():
    fedora = _linux(tools=("python3", "git"))
    fedora = Probes(**{**fedora.__dict__, "os_release": lambda: "ID=fedora\n"})
    assert _by_name(check_prerequisites(fedora))["tmux"]["fix"] == "sudo dnf install -y tmux"
    mac = Probes(
        system=lambda: "Darwin", which=lambda name: None if name == "tmux" else f"/usr/bin/{name}",
        run=lambda command: _done("3.11\n"), user=lambda: "elena", os_release=lambda: "",
        keyring_state=lambda: (True, "keyring.backends.macOS.Keyring"),
    )
    result = check_prerequisites(mac)
    assert result["os"] == "macos" and result["missing"] == ["tmux"] and result["needs_admin"] == []
    assert _by_name(result)["tmux"]["fix"] == "brew install tmux"
    assert "linger" not in _by_name(result)


def test_an_old_python_or_no_venv_module_is_named():
    old = Probes(**{**_linux().__dict__, "run": lambda command: _done("3.8\n") if command[0].endswith("python3") else _done("yes\n")})
    assert _by_name(check_prerequisites(old))["python"]["found"] is False
    no_venv = Probes(**{**_linux().__dict__, "run": lambda command: _done(code=1) if command[0].endswith("python3") else _done("yes\n")})
    item = _by_name(check_prerequisites(no_venv))["python"]
    assert item["found"] is False and item["fix"] == "sudo apt install -y python3-venv"


def test_status_puts_the_machine_first_on_a_new_machine(tmp_path):
    missing = check_prerequisites(_linux(tools=("python3", "git"), linger="no"))
    status = first_run_status(config=str(tmp_path / "absent.json"), targets_root=tmp_path, prerequisites=missing)
    assert status["state"] == MACHINE_NOT_CONFIGURED
    assert status["machine"]["prerequisites"]["missing"] == ["tmux", "linger"]
    # The step keyed to the state is unchanged; the machine comes before it.
    assert status["next"]["step"] == "configure_target"
    before = status["next"]["before"]
    assert before["step"] == "prepare_machine" and before["approval"] == "admin"
    assert before["missing"] == ["tmux", "linger"] and before["needs_admin"] == ["tmux", "linger"]
    assert "any administrator account on this machine" in before["explain"]
    ready = first_run_status(config=str(tmp_path / "absent.json"), targets_root=tmp_path, prerequisites=check_prerequisites(_linux()))
    assert ready["next"]["step"] == "configure_target" and "before" not in ready["next"]


def test_procedure_install_records_the_pb_that_installed_it(tmp_path):
    installer = {"pb": "/home/elena/pb-boot/bin/pb", "version": "2026.9.27.1507", "python": "/home/elena/pb-boot/bin/python"}
    installed = install_agent_procedure(["claude-code"], home=tmp_path, installed_by=installer)
    skill = tmp_path / ".claude" / "skills" / "problem-board-worker"
    record = json.loads((skill / INSTALLED_BY).read_text(encoding="utf-8"))
    assert record == {"schema": "problem-board.procedure-installed-by.v1", **installer}
    assert installed[0]["installed_by"] == str(skill / INSTALLED_BY)
    # The record sits outside the verified release tree: installing again is current.
    again = install_agent_procedure(["claude-code"], home=tmp_path, installed_by=installer)
    assert again[0]["state"] == "current"


def test_first_run_has_one_session_set_up_and_enroll_and_finds_pb():
    first_run = " ".join((source_package_path() / "references" / "first-run.md").read_text(encoding="utf-8").split())
    for piece in (
        "## One Session Sets Up, Then Becomes The Worker",
        # The same words as the README and the board's Connect a machine panel.
        'ALIAS=<name>@<machine>',
        'tmux new-session -d -s "$ALIAS" "mkdir -p ~/.kdcube/pb/workspaces/$ALIAS && cd ~/.kdcube/pb/workspaces/$ALIAS && claude --add-dir ~/.kdcube --dangerously-skip-permissions --disallowedTools AskUserQuestion"',
        'tmux attach -t "$ALIAS"',
        "Edit only the ALIAS line, for example ana@mint. Use letters, digits, '-' and '@': tmux does not allow '.' and ':' in a session name.",
        "Help me set up Problem Board on this machine, then enroll this session as a Problem Board worker with alias <name>@<machine>",
        "whatever session enrolls becomes the worker",
        "**A session started without that line**",
        '"Read outside the working directories"',
        "`installed-by.json` beside `SKILL.md`",
        "## `prepare_machine`: This Machine First",
        "any administrator account on this machine can run it",
        "## Tell The Person Plainly",
        "stand alone at the end of your reply",
        "source versions",
        'Do not ask the person for "the approved version"',
        "**Connect a machine** in the board's top bar",
        "as this machine reaches it",
        "`work_setup_endpoint_not_board`",
        "`work_setup_endpoint_unreachable`",
    ):
        assert piece in first_run, piece


def test_connect_a_machine_is_named_where_it_is_the_boards_top_bar():
    """Walk finding 17 (2026-09-27): the agent sent the person to the Project dialog; it is in the top bar."""

    from project_board.client import endpoint_check

    package = source_package_path().parents[2].parent
    readme = " ".join((package / "README.md").read_text(encoding="utf-8").split())
    first_run = " ".join((source_package_path() / "references" / "first-run.md").read_text(encoding="utf-8").split())
    for text in (readme, first_run, endpoint_check.WHERE_TO_COPY):
        assert "Connect a machine" in text and "top bar" in text
        assert "Project dialog" not in text.split("Connect a machine", 1)[1][:80]
