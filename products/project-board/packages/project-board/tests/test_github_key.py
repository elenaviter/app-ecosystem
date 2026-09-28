"""The agent's GitHub access through its owner's key (W371): the pure parts and the helper command."""

from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

import pytest

from project_board.client import cli
from project_board.client.github_key import (
    GitHubKeyRefused,
    GitHubToken,
    card_repositories,
    clone_config,
    credential_answer,
    credential_repository,
    gh_repository,
    helper_command,
    issue_token,
    issue_url,
    parse_credential_request,
    repository_name,
)

ON_CARD = {"example-org/app-ecosystem"}


def test_repository_names_come_from_any_github_spelling_and_nothing_else():
    assert repository_name("example-org/app-ecosystem") == "example-org/app-ecosystem"
    assert repository_name("https://github.com/example-org/app-ecosystem.git") == "example-org/app-ecosystem"
    assert repository_name("git@github.com:example-org/app-ecosystem.git") == "example-org/app-ecosystem"
    assert repository_name("https://gitlab.com/example-org/app-ecosystem") == ""
    assert repository_name("https://github.com/a/b/c") == ""
    assert card_repositories([{"url": "git@github.com:Example-Org/App.git"}, {"url": "/srv/x.git"}]) == {"example-org/app"}


def test_the_issue_route_is_connection_hubs_on_the_boards_platform():
    assert issue_url("https://kdcube.example/api/integrations/bundles/t/p/problem-board@1-0/public/mcp/x", "t", "p") == (
        "https://kdcube.example/api/integrations/bundles/t/p/connection-hub@1-0/public/project_agent_github_token_issue"
    )


def _post(status, body, seen):
    async def post(url, payload, bearer):
        seen.append((url, payload, bearer))
        return status, body

    return post


def test_an_issued_token_is_read_from_either_envelope_and_never_printed():
    seen: list = []
    body = {"project_agent_github_token_issue": {"ok": True, "token": "ghu_x", "expires_at": 9, "login": "o", "commit_email": "o@example.test", "repository": "example-org/app-ecosystem"}}
    token = asyncio.run(issue_token(post=_post(200, body, seen), url="u", bearer="b", project_ref="work:project:q", repository="example-org/app-ecosystem"))
    assert token.token == "ghu_x" and token.commit_email == "o@example.test"
    assert "ghu_x" not in repr(token)
    assert seen == [("u", {"data": {"project_ref": "work:project:q", "repository": "example-org/app-ecosystem"}}, "b")]


@pytest.mark.parametrize(
    "status, body, code",
    [
        (200, {"ok": False, "error": "github_not_linked", "message": "Your owner has not connected GitHub on this project."}, "github_not_linked"),
        (404, {}, "github_key_route_missing"),
        (401, {}, "github_key_card_refused"),
        (200, {"ok": True}, "http_200"),
    ],
)
def test_refusals_are_named(status, body, code):
    with pytest.raises(GitHubKeyRefused) as refused:
        asyncio.run(issue_token(post=_post(status, body, []), url="u", bearer="b", project_ref="p", repository="o/r"))
    assert refused.value.code == code


def test_the_credential_helper_answers_only_github_repositories_on_the_card():
    request = parse_credential_request("protocol=https\nhost=github.com\npath=example-org/app-ecosystem.git\n\n")
    assert credential_repository(request, ON_CARD) == "example-org/app-ecosystem"
    for text, code in (
        ("protocol=https\nhost=gitlab.com\npath=example-org/app-ecosystem.git\n", "github_key_host_refused"),
        ("protocol=http\nhost=github.com\npath=example-org/app-ecosystem.git\n", "github_key_host_refused"),
        ("protocol=https\nhost=github.com\n", "github_key_path_missing"),
        ("protocol=https\nhost=github.com\npath=example-org/private.git\n", "repository_not_on_card"),
    ):
        with pytest.raises(GitHubKeyRefused) as refused:
            credential_repository(parse_credential_request(text), ON_CARD)
        assert refused.value.code == code
    token = GitHubToken(token="ghu_x", expires_at=1700000000, login="o", commit_email="", repository="r")
    assert credential_answer(token) == "username=x-access-token\npassword=ghu_x\npassword_expiry_utc=1700000000\n"


def test_the_helper_line_and_clone_config_name_this_session_and_clear_inherited_helpers():
    helper = helper_command("claude-code", "abc-123")
    assert helper == "!pb worker git-credential --runtime-kind claude-code --runtime-session-id abc-123"
    assert clone_config(helper, name="agent", email="o@example.test") == [
        ["config", "--replace-all", "credential.https://github.com.helper", ""],
        ["config", "--add", "credential.https://github.com.helper", helper],
        ["config", "credential.https://github.com.useHttpPath", "true"],
        ["config", "user.name", "agent"],
        ["config", "user.email", "o@example.test"],
    ]


def test_gh_acts_on_its_repo_flag_else_the_clones_origin():
    assert gh_repository(["pr", "create", "-R", "example-org/x"], "") == "example-org/x"
    assert gh_repository(["pr", "list", "--repo=example-org/y"], "") == "example-org/y"
    assert gh_repository(["pr", "list"], "git@github.com:example-org/z.git") == "example-org/z"
    assert gh_repository(["pr", "list"], "") == ""


class _Session:
    on_card = ON_CARD

    def __init__(self, args):
        pass

    def token(self, repository):
        return GitHubToken(token="ghu_x", expires_at=0, login="o", commit_email="", repository=repository)


def test_the_git_credential_command_answers_get_and_refuses_on_stderr_without_stdout(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_GitHubKeySession", _Session)
    out = io.StringIO()
    code = cli._git_credential_command(  # noqa: SLF001
        SimpleNamespace(action="get"),
        stdin=io.StringIO("protocol=https\nhost=github.com\npath=example-org/app-ecosystem.git\n\n"),
        stdout=out,
    )
    assert code == 0 and out.getvalue() == "username=x-access-token\npassword=ghu_x\n"

    refused = io.StringIO()
    cli._git_credential_command(  # noqa: SLF001
        SimpleNamespace(action="get"),
        stdin=io.StringIO("protocol=https\nhost=github.com\npath=example-org/private.git\n\n"),
        stdout=refused,
    )
    assert refused.getvalue() == ""
    assert "repository_not_on_card" in capsys.readouterr().err

    stored = io.StringIO()
    assert cli._git_credential_command(SimpleNamespace(action="store"), stdin=io.StringIO("password=x\n"), stdout=stored) == 0  # noqa: SLF001
    assert stored.getvalue() == ""


def test_the_card_bearer_rides_its_own_header_never_authorization():
    """The first real push, 2026-09-28: Authorization made the platform demand a Card operation no Card can hold."""

    from project_board.client import github_key

    assert github_key.CARD_BEARER_HEADER == "X-Connection-Hub-Card-Bearer"
    source = (__import__("pathlib").Path(cli.__file__)).read_text(encoding="utf-8")
    post = source[source.index("async def _post_json"):source.index("def _git_credential_command")]
    assert "CARD_BEARER_HEADER: bearer" in post
    assert "Authorization" not in post
