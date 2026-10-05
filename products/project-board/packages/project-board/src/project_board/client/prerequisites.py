"""What this machine needs before Problem Board runs on it, per operating system (W304).

A stranger's new machine walk (operator, 2026-09-27, Linux Mint over ssh)
found that nothing checked the machine: tmux was missing, the user's services
did not survive logout (linger off) and the keyring was not usable over ssh,
and the person did not know that some fixes need an administrator while any
administrator account on the machine can run them.

`pb status` reports each prerequisite here: found or not, why it is needed,
what still works without it, the exact line that fixes it, and whether that
line needs an administrator. Checks only read: no package is installed, no
keyring entry is written, no setting changes. Every probe is injectable so
tests run on any machine.
"""

from __future__ import annotations

import getpass
import platform
import shutil
import subprocess
import threading

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

SCHEMA = "problem-board.prerequisites.v1"
MIN_PYTHON = (3, 10)
PROBE_TIMEOUT_SECONDS = 3.0
# W558: the keyring probe gives up instead of waiting for a prompt nobody sees.
KEYRING_TIMEOUT_SECONDS = 5.0
ADMIN_NOTE = (
    "Needs an administrator. Any administrator account on this machine can run it, for example "
    "after `su - <admin-user>`; your own account does not need to be one."
)
# The W258 unlock line for a headless Linux session (add-a-worker-host step 6).
# W558: it first stops a systemd user unit (and any leftover daemon) that would
# keep the Secret Service name on a locked store, and it prompts, because a
# silent read looked like nothing happened (mint, 2026-10-05).
KEYRING_UNLOCK_LINE = (
    "systemctl --user stop gnome-keyring-daemon.socket gnome-keyring-daemon.service 2>/dev/null; "
    "pkill -u \"$USER\" -x gnome-keyring-d; "
    "printf 'Password store password (not shown): '; read -rs P; echo; "
    "printf %s \"$P\" | gnome-keyring-daemon --replace --unlock --components=secrets >/dev/null; unset P"
)

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str] | None"]


def _run(command: Sequence[str]) -> "subprocess.CompletedProcess[str] | None":
    try:
        return subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


@dataclass(frozen=True)
class Probes:
    """What the checks read. Tests replace any of them."""

    system: Callable[[], str] = platform.system
    which: Callable[[str], str | None] = shutil.which
    run: Runner = _run
    user: Callable[[], str] = getpass.getuser
    os_release: Callable[[], str] = lambda: _read(Path("/etc/os-release"))
    keyring_state: Callable[[], tuple[bool, str]] = lambda: _keyring_state()


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _keyring_state(timeout: float = KEYRING_TIMEOUT_SECONDS) -> tuple[bool, str]:
    """Whether pb's own Python can use a password store now, without writing to it.

    W558 (mint, 2026-10-05): over SSH a locked or missing Secret Service store
    made `pb status` wait forever for an unlock prompt nobody could see. The
    probe never unlocks or creates a store, and it gives up after ``timeout``.
    """

    answer: list[tuple[bool, str]] = []
    probe = threading.Thread(target=lambda: answer.append(_keyring_state_now()), daemon=True)
    probe.start()
    probe.join(timeout)
    if not answer:
        return False, "the Secret Service does not answer in time (a locked store waiting for a prompt)"
    return answer[0]


def _keyring_state_now() -> tuple[bool, str]:
    try:
        import keyring  # noqa: PLC0415 - optional until the client extra is installed
    except Exception:  # noqa: BLE001
        return False, "the keyring package is not installed with pb"
    try:
        backend = keyring.get_keyring()
    except Exception as exc:  # noqa: BLE001
        return False, f"no password store backend ({type(exc).__name__})"
    name = f"{type(backend).__module__}.{type(backend).__name__}"
    if "fail" in name.lower() or "null" in name.lower():
        return False, f"no usable password store ({name})"
    if callable(getattr(backend, "get_preferred_collection", None)):
        # Secret Service (Linux). keyring's get_preferred_collection() unlocks a
        # locked collection, and secretstorage's get_default_collection()
        # creates a missing one: both prompt. Read the default collection and
        # its lock state only.
        try:
            import secretstorage  # noqa: PLC0415 - installed with keyring on Linux
            from secretstorage.collection import Collection  # noqa: PLC0415
            from secretstorage.exceptions import ItemNotFoundException  # noqa: PLC0415

            connection = secretstorage.dbus_init()
            try:
                collection = Collection(connection)
            except ItemNotFoundException:
                return False, "the Secret Service has no default store"
            if collection.is_locked():
                return False, "the login keyring is locked"
        except Exception as exc:  # noqa: BLE001
            return False, f"the Secret Service does not answer ({type(exc).__name__})"
    return True, name


def _linux_family(os_release: str) -> str:
    text = os_release.lower()
    if any(name in text for name in ("id=debian", "id=ubuntu", "id=linuxmint", "id_like=debian", "id_like=ubuntu", 'id_like="ubuntu')):
        return "debian"
    if any(name in text for name in ("id=fedora", "id_like=fedora", 'id_like="fedora', "id=rhel", 'id_like="rhel')):
        return "fedora"
    return ""


def _package_line(family: str, package: str) -> str:
    if family == "fedora":
        return f"sudo dnf install -y {package}"
    if family == "debian":
        return f"sudo apt install -y {package}"
    return f"install `{package}` with this system's package manager (sudo)"


def _item(
    name: str,
    found: bool,
    *,
    why: str,
    fix: str = "",
    admin: bool = False,
    without: str = "",
    detail: str = "",
    recommended: bool = False,
) -> dict[str, Any]:
    item: dict[str, Any] = {"name": name, "found": bool(found), "why": why}
    if recommended:
        # Worth having, never blocking: a missing one is named under
        # ``recommended``, not ``missing`` (W304 findings 23 and 24).
        item["recommended"] = True
    if detail:
        item["detail"] = detail
    if not found:
        item["fix"] = fix
        item["needs_admin"] = bool(admin)
        if admin:
            item["admin_note"] = ADMIN_NOTE
        if without:
            item["without_it"] = without
    return item


def _python(probes: Probes, family: str) -> dict[str, Any]:
    python = probes.which("python3")
    if not python:
        return _item(
            "python", False, why="pb and its relay run in a Python virtual environment.",
            fix=_package_line(family, "python3 python3-venv") if family else "install Python 3.10 or newer",
            admin=True,
        )
    probe = probes.run([python, "-c", "import sys, venv, ensurepip; print('%d.%d' % sys.version_info[:2])"])
    version = (probe.stdout.strip() if probe is not None and probe.returncode == 0 else "")
    parts = tuple(int(value) for value in version.split(".") if value.isdigit())
    if not version:
        return _item(
            "python", False, why="pb installs into a virtual environment, which needs Python's venv module.",
            fix=_package_line(family, "python3-venv"), admin=True, detail=f"{python} cannot create a virtual environment",
        )
    if parts < MIN_PYTHON:
        return _item(
            "python", False, why="pb needs Python 3.10 or newer.",
            fix="install Python 3.10 or newer", admin=True, detail=f"{python} is {version}",
        )
    return _item("python", True, why="pb and its relay run in a Python virtual environment.", detail=f"{python} {version}")


def _git(probes: Probes, family: str, *, mac: bool) -> dict[str, Any]:
    return _item(
        "git", bool(probes.which("git")), why="Agents clone the project's repositories.",
        fix="xcode-select --install" if mac else _package_line(family, "git"), admin=not mac,
    )


def _tmux(probes: Probes, family: str, *, mac: bool) -> dict[str, Any]:
    if mac:
        # Operator, 2026-09-27 (W304 finding 24): on a Mac the person is
        # usually at the machine, and a terminal tab is enough.
        return _item(
            "tmux", bool(probes.which("tmux")),
            why=(
                "Over ssh, each worker agent runs in its own tmux session, so it keeps running when the "
                "connection drops. At the machine, a terminal tab is enough."
            ),
            fix="brew install tmux",
            without="Agents run only while their terminal stays open.",
            recommended=True,
        )
    return _item(
        "tmux", bool(probes.which("tmux")),
        why="Each worker agent runs in its own tmux session, so it keeps running after you close ssh or the terminal.",
        fix=_package_line(family, "tmux"), admin=True,
        without="Agents run only while their terminal stays open.",
    )


def _gh(probes: Probes, family: str, *, mac: bool) -> dict[str, Any]:
    # W304 finding 23: deploy keys push branches; pull requests and review verdicts need gh.
    return _item(
        "gh", bool(probes.which("gh")),
        why=(
            "Agents open their pull requests and post review verdicts with gh, signed in with the GitHub "
            "identity the operator chose (add-a-worker-host step 7)."
        ),
        fix="brew install gh" if mac else _package_line(family, "gh"), admin=not mac,
        without="Agents push their branches, and the coordinator opens their pull requests.",
        recommended=True,
    )


def _linger(probes: Probes) -> dict[str, Any]:
    user = probes.user()
    answer = probes.run(["loginctl", "show-user", user, "--property=Linger", "--value"])
    if answer is None or answer.returncode != 0:
        # No session record yet (never logged in through logind) reads as no.
        found = False
    else:
        found = answer.stdout.strip().lower() == "yes"
    return _item(
        "linger", found,
        why="The relay is a systemd user service; linger keeps it running when you are not logged in.",
        fix=f"sudo loginctl enable-linger {user}", admin=True,
        without="The relay stops when your last session on this machine closes, and mail waits until you log in again.",
    )


def _systemd_user(probes: Probes) -> dict[str, Any]:
    answer = probes.run(["systemctl", "--user", "show-environment"])
    return _item(
        "systemd user manager", answer is not None and answer.returncode == 0,
        why="`pb relay-service install` installs the relay as a systemd user service.",
        fix="log in to this account once through ssh or the console, with linger on, so systemd starts your user manager",
        without="`pb relay-service install` cannot install the relay; `pb relay` still runs in the foreground.",
    )


def _keyring(probes: Probes, family: str, *, mac: bool) -> dict[str, Any]:
    usable, detail = probes.keyring_state()
    if mac:
        fix = "unlock the login keychain (open Keychain Access, or sign in to this account on the Mac once)"
        admin = False
    elif "locked" in detail or "does not answer" in detail or "no default store" in detail:
        fix = KEYRING_UNLOCK_LINE
        admin = False
    else:
        fix = _package_line(family, "gnome-keyring libsecret-1-0") if family == "debian" else _package_line(family, "gnome-keyring")
        admin = True
    return _item(
        "keyring", usable,
        why="Connection Hub keeps each agent's credential in the machine's password store, never in a file.",
        fix=fix, admin=admin, detail=detail,
        without="Agents cannot be authorized on this machine.",
    )


def check_prerequisites(probes: Probes | None = None) -> dict[str, Any]:
    """Every prerequisite for this operating system, the missing ones first named."""

    probes = probes or Probes()
    system = probes.system()
    mac = system == "Darwin"
    family = "" if mac else _linux_family(probes.os_release())
    if mac:
        checked = [
            _python(probes, family),
            _git(probes, family, mac=True),
            _tmux(probes, family, mac=True),
            _gh(probes, family, mac=True),
            _keyring(probes, family, mac=True),
        ]
    elif system == "Linux":
        checked = [
            _python(probes, family),
            _git(probes, family, mac=False),
            _tmux(probes, family, mac=False),
            _gh(probes, family, mac=False),
            _linger(probes),
            _systemd_user(probes),
            _keyring(probes, family, mac=False),
        ]
    else:
        return {"schema": SCHEMA, "os": system, "ok": False, "missing": [], "checked": [],
                "note": f"{system} is not a supported worker host; use Linux or macOS."}
    missing = [item["name"] for item in checked if not item["found"] and not item.get("recommended")]
    return {
        "schema": SCHEMA,
        "os": "macos" if mac else "linux",
        "ok": not missing,
        "missing": missing,
        "needs_admin": [item["name"] for item in checked if item["name"] in missing and item.get("needs_admin")],
        "recommended": [item["name"] for item in checked if not item["found"] and item.get("recommended")],
        "checked": checked,
    }


__all__ = ["ADMIN_NOTE", "KEYRING_UNLOCK_LINE", "Probes", "SCHEMA", "check_prerequisites"]
