"""pb status names a Python whose hashlib cannot build BLAKE2 (W933).

On macOS (2026-10-23) the release ran on a pyenv Python whose ``_blake2``
module links Homebrew's libb2, and libb2 was not installed. hashlib logged a
traceback for blake2b and blake2s at import, so every pb command, the device
login included, started with two of them, while ``pb status`` reported
``prerequisites: ok True · missing none``.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from project_board.client import release_smoke
from project_board.client.prerequisites import Probes, check_prerequisites, hashlib_prerequisite, hashlib_state
from project_board.client.render import render_envelope

LIBB2_MISSING = (
    False,
    "/home/u/venv/bin/python: blake2b and blake2s unavailable (dlopen(_blake2.cpython-313-darwin.so): "
    "Library not loaded: /opt/homebrew/opt/libb2/lib/libb2.1.dylib)",
)


def _probes(system: str, hashlib: tuple[bool, str], os_release: str = "") -> Probes:
    return Probes(
        system=lambda: system,
        which=lambda name: f"/usr/bin/{name}",
        run=lambda command: subprocess.CompletedProcess(["probe"], 0, "3.12\n" if command[0].endswith("python3") else "yes\n", ""),
        user=lambda: "person",
        os_release=lambda: os_release,
        keyring_state=lambda: (True, "keyring.backends.macOS.Keyring"),
        hashlib_state=lambda: hashlib,
    )


def _item(result, name):
    return {item["name"]: item for item in result["checked"]}[name]


def test_a_mac_without_libb2_is_not_ready_and_names_brew_install_libb2():
    result = check_prerequisites(_probes("Darwin", LIBB2_MISSING))
    assert result["ok"] is False and result["missing"] == ["hashlib"] and result["needs_admin"] == []
    item = _item(result, "hashlib")
    assert item["fix"] == "brew install libb2" and item["needs_admin"] is False
    assert "libb2.1.dylib" in item["detail"]
    assert "blake2 tracebacks" in item["without_it"]


def test_a_linux_host_gets_its_package_line_with_an_administrator():
    debian = check_prerequisites(_probes("Linux", LIBB2_MISSING, 'ID=linuxmint\nID_LIKE="ubuntu debian"\n'))
    assert _item(debian, "hashlib")["fix"] == "sudo apt install -y libb2-1"
    assert "hashlib" in debian["needs_admin"]
    fedora = check_prerequisites(_probes("Linux", LIBB2_MISSING, "ID=fedora\n"))
    assert _item(fedora, "hashlib")["fix"] == "sudo dnf install -y libb2"


def test_another_cause_names_a_reinstall_of_that_python():
    other = (False, "/x/python: blake2b and blake2s unavailable (No module named '_blake2')")
    item = _item(check_prerequisites(_probes("Darwin", other)), "hashlib")
    assert "pyenv install --force" in item["fix"] and item["needs_admin"] is False


def test_a_working_hashlib_is_found_and_nothing_is_missing():
    result = check_prerequisites(_probes("Darwin", (True, "/x/python: blake2b and blake2s load")))
    assert _item(result, "hashlib")["found"] is True and result["ok"] is True


def test_the_probe_reads_this_python():
    usable, detail = hashlib_state()
    assert usable is True and detail.startswith(sys.executable)


def test_the_probe_names_a_hashlib_that_could_not_build_blake2(monkeypatch):
    import hashlib

    monkeypatch.delattr(hashlib, "blake2b")
    monkeypatch.delattr(hashlib, "blake2s")
    usable, detail = hashlib_state()
    assert usable is False and "blake2b and blake2s unavailable" in detail


def test_present_but_broken_constructor_is_not_ready(monkeypatch):
    import hashlib

    def broken(*args, **kwargs):
        raise ValueError("BLAKE2 backend unavailable")

    monkeypatch.setattr(hashlib, "blake2b", broken)
    usable, detail = hashlib_state()
    assert usable is False and "blake2b unavailable (BLAKE2 backend unavailable)" in detail


def test_extension_must_load_even_when_hashlib_is_already_cached(monkeypatch):
    import hashlib

    assert hashlib.blake2b().digest()
    monkeypatch.setitem(sys.modules, "_blake2", None)
    usable, detail = hashlib_state()
    assert usable is False and "_blake2 unavailable" in detail


def test_a_real_python_whose_blake2_cannot_load_is_not_ready():
    # The same failure as a missing libb2: hashlib's import of _blake2 raises,
    # it logs the traceback, and blake2b and blake2s are left undefined.
    script = (
        "import sys; sys.modules['_blake2'] = None; import json; "
        "from project_board.client.prerequisites import Probes, check_prerequisites; "
        "result = check_prerequisites(Probes(keyring_state=lambda: (True, 'stub'))); "
        "print(json.dumps({'missing': result['missing'], "
        "'hashlib': [i for i in result['checked'] if i['name'] == 'hashlib'][0]}))"
    )
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    observed = json.loads(done.stdout.strip().splitlines()[-1])
    assert "hashlib" in observed["missing"]
    assert observed["hashlib"]["found"] is False
    assert "blake2b and blake2s unavailable" in observed["hashlib"]["detail"]
    assert "unsupported hash type blake2b" in done.stderr


@pytest.mark.parametrize("system, fix", [
    ("Darwin", "brew install libb2"),
    ("Linux", "sudo apt install -y libb2-1"),
])
def test_the_release_smoke_refuses_a_candidate_without_blake2(monkeypatch, system, fix):
    # Select the candidate platform through the public injectable probe, not
    # the test host's OS (the dataclass's default callable is bound on import).
    item = hashlib_prerequisite(_probes(system, LIBB2_MISSING, "ID=debian\n"))
    assert item["fix"] == fix
    monkeypatch.setattr("project_board.client.prerequisites.hashlib_prerequisite", lambda: item)
    with pytest.raises(ImportError, match="hashlib prerequisite missing") as error:
        release_smoke.verify_hashlib()
    assert f"Fix: {fix}" in str(error.value)
    monkeypatch.setattr("project_board.client.prerequisites.hashlib_prerequisite", lambda: {"found": True})
    release_smoke.verify_hashlib()


def test_a_configured_machine_status_prints_the_cause_impact_and_fix():
    prerequisites = check_prerequisites(_probes("Darwin", LIBB2_MISSING))
    text = render_envelope({"ok": True, "result": {
        "schema": "problem-board.first-run-status.v1", "state": "ready",
        "machine": {"prerequisites": prerequisites}, "session": {}, "next": {},
    }})
    assert "prerequisites: ok False · missing hashlib" in text
    assert "hashlib: /home/u/venv/bin/python:" in text
    assert "Library not loaded: /opt/homebrew/opt/libb2/lib/libb2.1.dylib" in text
    assert "Commands that do not use BLAKE2 can still succeed" in text
    assert "hashlib fix: brew install libb2" in text


def test_importing_the_candidate_smoke_rejects_a_broken_extension():
    # Cached hashlib constructors alone must not let the candidate activate.
    # This is a disposable import failure, not a change to the host's Python.
    done = subprocess.run([
        sys.executable, "-c",
        "import hashlib, sys; sys.modules['_blake2'] = None; "
        "import project_board.client.release_smoke",
    ], capture_output=True, text=True, check=False)
    assert done.returncode != 0
    assert "hashlib prerequisite missing:" in done.stderr
    assert "_blake2 unavailable" in done.stderr
    assert "reinstall this Python" in done.stderr
