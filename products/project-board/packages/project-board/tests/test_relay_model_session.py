from types import SimpleNamespace

import pytest

from project_board.client import relay


@pytest.mark.parametrize("cadence", [30, 60, 120])
def test_model_sample_rides_the_heartbeat_without_process_identity(monkeypatch, cadence):
    native_id = "worker-thread"
    listener = {"session_id": native_id, "state": "waiting", "presence": "waiting"}
    binding = {"session_id": native_id, "pid": 456, "start": "private", "bound_at": "2026-11-25T18:00:00Z"}
    adapter = object.__new__(relay.ProblemBoardHostRelayAdapter)
    adapter.config = SimpleNamespace(runtime_session_id=native_id, worker_name="worker")
    adapter.field = SimpleNamespace(read_worker=lambda _: {"model_session_binding": binding, "listener": listener})
    adapter._heartbeat_sent_at = {"project": 10}
    adapter._monotonic = lambda: 10 + cadence + 7
    monkeypatch.setattr(relay, "observe_session", lambda supplied: "running" if supplied == binding else "unknown")
    monkeypatch.setattr(relay, "utc_now", lambda: "2026-11-25T18:01:00Z")

    report = adapter._model_session_report("project", [listener], cadence)
    assert report["model_session"] == {
        "state": "running",
        "bound_at": "2026-11-25T18:00:00Z",
        "observed_at": "2026-11-25T18:01:00Z",
        "cadence_seconds": cadence,
        "jitter_seconds": 7,
    }
    assert "model_session" not in listener
    assert adapter._model_session_report("project", [{"session_id": "another"}], cadence) is None
    listener["state"] = "detached"
    report = adapter._model_session_report("project", [listener], cadence)
    assert report["model_session"]["state"] == "detached"
