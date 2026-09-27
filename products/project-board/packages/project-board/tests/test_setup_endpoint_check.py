"""`pb setup` checks that the endpoint answers as the board's MCP (W304, finding 14).

Operator, 2026-09-27: a stranger gets the board's connection values from the
board's "Connect a machine" panel, and `pb setup` checks the endpoint before it
writes anything. On the walk, the person almost gave setup the worker-stream
address found first in another machine's relay.json.
"""

from __future__ import annotations

import http.server
import json
import threading
import urllib.error
from pathlib import Path

import pytest

from project_board.client import cli, endpoint_check
from project_board.client.endpoint_check import verify_board_endpoint
from project_board.contract.errors import DomainError

BASE = "https://board.example"
ENDPOINT = f"{BASE}/api/integrations/bundles/t1/p1/problem-board@1-0/public/mcp/problem_board"
METADATA = f"{BASE}/api/integrations/bundles/t1/p1/connection-hub@1-0/public/oauth/.well-known/oauth-protected-resource?resource=x"
CHALLENGE = {"www-authenticate": f'Bearer resource_metadata="{METADATA}"'}


def _post(status: int, headers: dict[str, str] | None = None):
    return lambda _url: (status, headers or {})


def _fetch(document):
    return lambda _url: document


def test_the_board_endpoint_is_verified_without_a_credential() -> None:
    seen: list[str] = []

    def fetch(url: str):
        seen.append(url)
        return {"resource": ENDPOINT, "authorization_servers": [f"{BASE}/oauth"]}

    result = verify_board_endpoint(ENDPOINT + "/", post=_post(401, CHALLENGE), fetch=fetch)
    assert result["state"] == "verified"
    assert result["endpoint"] == ENDPOINT
    assert seen == [METADATA]


@pytest.mark.parametrize(
    ("post", "document", "found"),
    [
        # The worker-stream address from relay.json answers 404 to an initialize.
        (_post(404), None, "HTTP 404"),
        (_post(200), None, "HTTP 200"),
        (_post(401, {"www-authenticate": "Bearer"}), None, "without a resource_metadata challenge"),
        (_post(401, CHALLENGE), {"resource": ENDPOINT.replace("problem_board", "worker_stream")}, "another resource"),
        (_post(401, CHALLENGE), ["not", "an", "object"], "another resource"),
    ],
)
def test_anything_else_is_refused_with_the_expected_shape(post, document, found) -> None:
    with pytest.raises(DomainError) as refused:
        verify_board_endpoint(ENDPOINT, post=post, fetch=_fetch(document))
    assert refused.value.code == "work_setup_endpoint_not_board"
    assert found in refused.value.details["found"]
    assert "/problem-board@1-0/public/mcp/problem_board" in str(refused.value)
    assert "Connect a machine" in str(refused.value)


def test_metadata_on_another_server_is_not_followed() -> None:
    elsewhere = {"www-authenticate": 'Bearer resource_metadata="https://elsewhere.example/meta"'}

    def fetch(_url):
        raise AssertionError("must not fetch another server's metadata")

    with pytest.raises(DomainError) as refused:
        verify_board_endpoint(ENDPOINT, post=_post(401, elsewhere), fetch=fetch)
    assert refused.value.code == "work_setup_endpoint_not_board"


def test_an_endpoint_this_machine_cannot_reach_is_named() -> None:
    def post(_url):
        raise urllib.error.URLError("connection refused")

    with pytest.raises(DomainError) as refused:
        verify_board_endpoint(ENDPOINT, post=post)
    assert refused.value.code == "work_setup_endpoint_unreachable"
    assert "as this machine reaches the server" in str(refused.value)


class _Board(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:  # quiet
        return None

    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        base = f"http://127.0.0.1:{self.server.server_port}"
        if self.path.endswith("/public/mcp/problem_board"):
            self.send_response(401)
            self.send_header("WWW-Authenticate", f'Bearer resource_metadata="{base}/meta"')
        else:
            self.send_response(404)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        base = f"http://127.0.0.1:{self.server.server_port}"
        body = json.dumps({"resource": f"{base}/b/problem-board@1-0/public/mcp/problem_board"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def board():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Board)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def test_the_real_http_round_trip(board) -> None:
    assert verify_board_endpoint(f"{board}/b/problem-board@1-0/public/mcp/problem_board")["state"] == "verified"
    with pytest.raises(DomainError) as refused:
        verify_board_endpoint(f"{board}/b/problem-board@1-0/public/mcp/worker_stream")
    assert refused.value.code == "work_setup_endpoint_not_board"


def _setup_argv(tmp_path: Path, *extra: str) -> list[str]:
    root = tmp_path / "workspaces"
    root.mkdir(exist_ok=True)
    return [
        "--format", "json", "setup",
        "--target-id", "t", "--endpoint", ENDPOINT, "--tenant", "t1", "--platform-project", "p1",
        "--host-id", "h", "--allow-root", str(root),
        "--config", str(tmp_path / "relay.json"),
        "--state-root", str(tmp_path / "state"),
        "--connection-hub-state-root", str(tmp_path / "hub"),
        *extra,
    ]


def test_setup_writes_nothing_when_the_endpoint_is_refused(tmp_path, monkeypatch, capsys) -> None:
    def refuse(endpoint, **_):
        raise DomainError("work_setup_endpoint_not_board", "no", details={"endpoint": endpoint})

    monkeypatch.setattr(endpoint_check, "verify_board_endpoint", refuse)
    assert cli.main(_setup_argv(tmp_path)) == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "work_setup_endpoint_not_board"
    assert not (tmp_path / "relay.json").exists()
    assert not (tmp_path / "state").exists()


def test_setup_reports_the_check_and_can_skip_it_offline(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        endpoint_check, "verify_board_endpoint", lambda endpoint, **_: {"state": "verified", "endpoint": endpoint}
    )
    assert cli.main(_setup_argv(tmp_path)) == 0
    assert json.loads(capsys.readouterr().out)["result"]["endpoint_check"]["state"] == "verified"

    def refuse(*_args, **_kwargs):
        raise AssertionError("--no-verify-endpoint must not probe")

    monkeypatch.setattr(endpoint_check, "verify_board_endpoint", refuse)
    other = tmp_path / "offline"
    other.mkdir()
    assert cli.main(_setup_argv(other, "--no-verify-endpoint")) == 0
    assert json.loads(capsys.readouterr().out)["result"]["endpoint_check"] == {"state": "skipped", "endpoint": ENDPOINT}
    assert (other / "relay.json").exists()


# W304 finding 18 (operator, mid-walk 2026-09-27): the endpoint names the
# tenant and the platform project, so pb setup reads them from it.


def test_the_tenant_and_project_are_read_from_the_endpoint() -> None:
    assert endpoint_check.setup_scope(ENDPOINT) == ("t1", "p1")
    assert endpoint_check.setup_scope(ENDPOINT + "/") == ("t1", "p1")
    # A value given as well must agree.
    assert endpoint_check.setup_scope(ENDPOINT, tenant="t1", platform_project="p1") == ("t1", "p1")


def test_a_value_that_differs_from_the_endpoint_is_refused_by_name() -> None:
    with pytest.raises(DomainError) as refused:
        endpoint_check.setup_scope(ENDPOINT, tenant="other", platform_project="p1")
    assert refused.value.code == "work_setup_scope_mismatch"
    assert refused.value.details["tenant"] == {"given": "other", "endpoint": "t1"}
    assert "platform_project" not in refused.value.details


@pytest.mark.parametrize(
    "endpoint",
    [
        f"{BASE}/api/integrations/bundles/t1/p1/problem-board@1-0/public/worker_stream",
        f"{BASE}/mcp",
        f"{BASE}/api/integrations/bundles/t1/problem-board@1-0/public/mcp/problem_board",
    ],
)
def test_an_endpoint_that_is_not_a_board_bundle_path_is_refused(endpoint) -> None:
    with pytest.raises(DomainError) as refused:
        endpoint_check.setup_scope(endpoint)
    assert refused.value.code == "work_setup_endpoint_not_bundle"
    assert endpoint_check.EXPECTED_SHAPE in str(refused.value)


def test_setup_needs_only_the_endpoint_and_the_persons_own_names(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        endpoint_check, "verify_board_endpoint", lambda endpoint, **_: {"state": "verified", "endpoint": endpoint}
    )
    argv = [value for value in _setup_argv(tmp_path) if value not in {"--tenant", "t1", "--platform-project", "p1"}]
    assert "--tenant" not in argv and "--platform-project" not in argv
    assert cli.main(argv) == 0
    capsys.readouterr()
    target = json.loads((tmp_path / "relay.json").read_text(encoding="utf-8"))["target"]
    assert (target["tenant"], target["project"]) == ("t1", "p1")
    # A differing value refuses before anything is written.
    other = tmp_path / "mismatch"
    other.mkdir()
    mismatch = _setup_argv(other)
    mismatch[mismatch.index("--tenant") + 1] = "other"
    assert cli.main(mismatch) == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "work_setup_scope_mismatch"
    assert not (other / "relay.json").exists()
