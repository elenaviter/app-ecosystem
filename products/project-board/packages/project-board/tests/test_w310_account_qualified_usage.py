"""W310: a usage sample names the host login it was read under, when that is known.

The operator's case (2026-09-29): a session started under account A, then the
machine logged in to account B, and B's quota showed as the session's
capacity. The relay can only read the host's current login file, so a sample
is qualified with that login only when the login file last changed before the
sample was taken. A later login never relabels an earlier sample.

All ids are synthetic. The login files carry no credential the tests read.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace

from project_board.client import cli
from project_board.client import relay as relay_module
from project_board.client.limit_state import qualify_limit_state
from project_board.client.render import (
    _runtime_account_brief,
    _team_usage_lines,
)
from project_board.client.runtime_account import read_host_login
from project_board.client.store import SharedFieldStore

from relay_helpers import make_host

SESSION = "00000000-0000-4000-8000-00000000a310"


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _jwt(claims: dict[str, object]) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def _codex_login(home, account_id: str, *, changed_at: str) -> None:
    path = home / ".codex" / "auth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tokens": {"account_id": account_id, "id_token": _jwt({})}}), encoding="utf-8")
    os.utime(path, (_epoch(changed_at), _epoch(changed_at)))


def _claude_login(home, account_id: str, *, changed_at: str) -> None:
    path = home / ".claude.json"
    path.write_text(json.dumps({"oauthAccount": {"accountUuid": account_id}}), encoding="utf-8")
    os.utime(path, (_epoch(changed_at), _epoch(changed_at)))


def _rollout(root, *, observed_at: str) -> None:
    day = root / "2026" / "09" / "29"
    day.mkdir(parents=True, exist_ok=True)
    limits = {"primary": {"used_percent": 12.0, "window_minutes": 300, "resets_at": 1790010000}, "secondary": None}
    lines = [
        json.dumps({"timestamp": "2026-09-29T11:00:00.000Z", "type": "session_meta", "payload": {"id": SESSION}}),
        json.dumps({"timestamp": observed_at, "type": "event_msg", "payload": {"type": "token_count", "info": {}, "rate_limits": limits}}),
    ]
    (day / f"rollout-2026-09-29T11-00-00-{SESSION}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- the rule


def test_the_host_login_reader_returns_the_account_id_and_when_the_file_changed(tmp_path):
    _codex_login(tmp_path, "acct-a", changed_at="2026-09-29T11:30:00Z")
    login = read_host_login("codex", home=tmp_path)
    assert login == {"account_id": "acct-a", "changed_at": "2026-09-29T11:30:00.000000Z"}
    assert "id_token" not in json.dumps(login)
    assert read_host_login("codex", home=tmp_path / "nobody") == {}
    assert read_host_login("other-runtime", home=tmp_path) == {}


def test_a_sample_names_the_login_only_when_the_login_came_first():
    sample = {"kind": "ok", "observed_at": "2026-09-29T12:00:00Z", "windows": []}
    before = {"account_id": "acct-a", "changed_at": "2026-09-29T11:30:00Z"}
    after = {"account_id": "acct-b", "changed_at": "2026-09-29T12:10:00Z"}
    same_second = {"account_id": "acct-b", "changed_at": "2026-09-29T12:00:00.400000Z"}
    assert qualify_limit_state(sample, before)["account_id"] == "acct-a"
    # A later login never relabels an earlier sample.
    assert "account_id" not in qualify_limit_state(sample, after)
    assert "account_id" not in qualify_limit_state(sample, same_second)
    assert "account_id" not in qualify_limit_state(sample, {})
    # A sample that already names its account keeps it.
    named = {**sample, "account_id": "acct-a"}
    assert qualify_limit_state(named, after)["account_id"] == "acct-a"


# ---------------------------------------------------------------- Codex: the relay


class _Field:
    def worker_listener_session(self, name):
        return {"session_id": SESSION, "state": "listening"}

    def runtime_limit_state(self, name):
        return {}

    def wake_hold(self, name):
        return {}

    def runtime_model(self, name):
        return {}

    def record_runtime_model(self, name, record):
        return dict(record)


def _codex_adapter(monkeypatch, sessions_root):
    from project_board.client import limit_state as limit_state_module

    real = limit_state_module.codex_rollout_path
    monkeypatch.setattr(
        limit_state_module,
        "codex_rollout_path",
        lambda session_id, sessions_root=None: real(session_id, sessions_root=sessions_root_dir),
    )
    sessions_root_dir = sessions_root
    adapter = relay_module.ProblemBoardHostRelayAdapter.__new__(relay_module.ProblemBoardHostRelayAdapter)
    adapter.config = SimpleNamespace(worker_name="codex-w310", runtime_kind="codex", runtime_session_id=SESSION)
    adapter.field = _Field()
    return adapter


def test_a_codex_session_under_a_keeps_its_sample_when_the_host_logs_in_to_b(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    sessions = tmp_path / "sessions"
    _rollout(sessions, observed_at="2026-09-29T12:00:00.000Z")
    adapter = _codex_adapter(monkeypatch, sessions)

    # The host was logged in to A before the sample: the sample is A's.
    _codex_login(home, "acct-a", changed_at="2026-09-29T11:00:00Z")
    [row] = adapter._listener_sessions()  # noqa: SLF001
    assert row["limit_state"]["account_id"] == "acct-a"

    # The host logs in to B after the sample. The same sample is not relabeled B.
    _codex_login(home, "acct-b", changed_at="2026-09-29T12:05:00Z")
    [row] = adapter._listener_sessions()  # noqa: SLF001
    assert "account_id" not in row["limit_state"]

    # A sample taken after the switch is read under B, and names B.
    for path in sessions.glob("*/*/*/rollout-*.jsonl"):
        path.unlink()
    _rollout(sessions, observed_at="2026-09-29T12:10:00.000Z")
    [row] = adapter._listener_sessions()  # noqa: SLF001
    assert row["limit_state"]["account_id"] == "acct-b"


# ---------------------------------------------------------------- Claude Code: the recorder


def test_the_claude_code_recorder_names_the_login_each_sample_was_read_under(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    host, identity, _channel = make_host(tmp_path)
    field = SharedFieldStore(host.field_root)
    field.initialize(field_id="w310-recorder")
    field.register_worker(
        worker_name=identity.worker_name,
        worker_identity=identity.worker_identity,
        runtime_kind=identity.runtime_kind,
        runtime_session_id=identity.runtime_session_id,
        capabilities=[],
        authority_label="connection-hub:test-profile",
        control_plane_state="published",
    )
    args = argparse.Namespace(
        config=str(host.path),
        runtime_kind=identity.runtime_kind,
        runtime_session_id=identity.runtime_session_id,
        source="statusline",
        payload_file="-",
    )
    payload = {"session_id": "s", "rate_limits": {"five_hour": {"used_percentage": 12, "resets_at": 1790010000}}}

    _claude_login(home, "acct-a", changed_at="2026-09-29T11:00:00Z")
    assert cli._limit_state_command(args, stdin=io.StringIO(json.dumps(payload))) == 0
    assert field.runtime_limit_state(identity.worker_name)["account_id"] == "acct-a"

    # The host logs in to B. The next sample is read under B and says so,
    # even though its figures did not change.
    _claude_login(home, "acct-b", changed_at="2026-09-29T11:30:00Z")
    assert cli._limit_state_command(args, stdin=io.StringIO(json.dumps(payload))) == 0
    assert field.runtime_limit_state(identity.worker_name)["account_id"] == "acct-b"

    # No readable login: the sample names no account.
    (home / ".claude.json").unlink()
    assert cli._limit_state_command(args, stdin=io.StringIO(json.dumps(payload))) == 0
    capsys.readouterr()


# ---------------------------------------------------------------- the brief read


def test_the_team_usage_line_says_when_a_sample_is_not_the_sessions_capacity():
    now = datetime(2026, 9, 29, 12, 30, tzinfo=timezone.utc)
    sample = {"kind": "ok", "source": "codex-rollout", "observed_at": "2026-09-29T12:10:00Z",
              "windows": [{"name": "primary", "used_percent": 12.0, "window_minutes": 300, "resets_at": "2026-09-29T15:00:00Z"}]}
    team = [
        {"worker_name": "codex-a", "limit_state": {**sample, "attribution": "session"}},
        {"worker_name": "codex-b", "limit_state": {**sample, "attribution": "host_login"}},
        {"worker_name": "codex-c", "limit_state": {**sample, "attribution": "unverified"}},
        {"worker_name": "codex-old", "limit_state": dict(sample)},
    ]
    lines = _team_usage_lines(team, now=now)
    [a] = [line for line in lines if "codex-a" in line]
    [b] = [line for line in lines if "codex-b" in line]
    [c] = [line for line in lines if "codex-c" in line]
    [old] = [line for line in lines if "codex-old" in line]
    assert "capacity" not in a
    assert "not this session's capacity: read under the host's other login" in b
    assert "not this session's capacity: the account it was read under is not known" in c
    # A board older than W310 sends no attribution: the line stays as it was.
    assert "capacity" not in old


def test_the_account_line_names_a_mismatch_in_plain_words():
    member = {
        "runtime_account": {"account_id": "acct-a", "source": "host-report", "observed_at": "2026-09-29T12:00:00Z", "state": "reported"},
        "account_state": "mismatch",
    }
    line = _runtime_account_brief(member)
    assert "account_id acct-a" in line
    assert "the host is now logged in to another account" in line
    unknown = _runtime_account_brief({**member, "account_state": "unknown"})
    assert "not known" in unknown
    legacy = _runtime_account_brief({k: v for k, v in member.items() if k != "account_state"})
    assert "logged in" not in legacy
