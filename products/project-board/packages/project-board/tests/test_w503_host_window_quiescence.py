"""Exercise the procedure's START guard without a board or host action."""

from pathlib import Path
import re
import subprocess

import pytest
from procedure_reference import reference_text


REFERENCES = (
    Path(__file__).parents[1]
    / "src/project_board/procedures/problem-board-worker/references"
)


def _start_guard() -> str:
    text = (REFERENCES / "runtime-actions.md").read_text()
    match = re.search(r"<!-- host-start-send-guard -->\s*```bash\n(.*?)\n```", text, re.S)
    assert match, "The owning procedure needs an executable fail-closed START guard"
    return match.group(1)


@pytest.mark.parametrize("refused_worker", ["first", "second", "third"])
@pytest.mark.parametrize("exit_code", [1, 2])
def test_a_required_start_refusal_never_executes_the_action(refused_worker, exit_code):
    result = _run_guard(refused_worker, exit_code)
    assert result.returncode != 0
    assert "EXECUTED" not in result.stdout
    sends = result.stdout.splitlines()
    participants = ["first", "second", "third"]
    assert sends == ["SEND " + name for name in participants[: participants.index(refused_worker) + 1]]
    assert "Window not started" in result.stderr


def _run_guard(refused_worker="none", exit_code=0, workers="first second third"):
    # pb is a shell stub: no subprocess here can reach the board or restart a host.
    stub = f"""
pb() {{
  while [ "$#" -gt 0 ]; do
    if [ "$1" = --recipient ]; then recipient=$2; break; fi
    shift
  done
  printf 'SEND %s\\n' "$recipient"
  if [ "$recipient" = '{refused_worker}' ]; then return {exit_code}; fi
  return 0
}}
project_ref=project
work_ref=item
window_id=window
start_body_file=body.md
affected_workers=({workers})
"""
    return subprocess.run(
        ["bash", "-c", stub + _start_guard() + "\nprintf 'EXECUTED\\n'"],
        text=True, capture_output=True, timeout=5,
    )


def test_all_required_sends_succeed_before_the_following_step():
    result = _run_guard()
    assert result.returncode == 0
    assert result.stdout.splitlines() == ["SEND first", "SEND second", "SEND third", "EXECUTED"]


def test_a_missing_participant_inventory_cannot_start_a_window():
    result = _run_guard(workers="")
    assert result.returncode != 0
    assert "EXECUTED" not in result.stdout


def test_regression_detects_an_unchecked_send_failure(monkeypatch):
    unchecked = _start_guard().replace(' || exit 1', '')
    monkeypatch.setitem(globals(), "_start_guard", lambda: unchecked)
    with pytest.raises(AssertionError):
        test_a_required_start_refusal_never_executes_the_action("second", 2)


def test_host_quiescence_has_one_owner_and_no_runtime_only_waiver():
    owner = " ".join((REFERENCES / "runtime-actions.md").read_text().split())
    assert "## Host Client Window Quiescence" in owner
    for invariant in (
        "READY means calls drained and held",
        "acknowledged STARTING NOW",
        "idle/waiting session state",
        "bounded UTC acknowledgement deadline",
        "non-quiesced",
        "Transport acceptance is not session handling",
        "a quiet relay at one instant",
        "original identity and idempotency key",
    ):
        assert invariant in owner
    for name in ("test-window.md", "coordinator.md", "collaboration.md"):
        assert "runtime-actions.md#host-client-window-quiescence" in reference_text(REFERENCES / name)
    window = (REFERENCES / "test-window.md").read_text()
    assert "A reload, refresh or client switch loads" not in window
    coordinator = reference_text(REFERENCES / "coordinator.md")
    assert "This missing-answer waiver never applies to a host client window" in coordinator
