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
    gh_forwarded_args,
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


def test_ssh_alias_origins_name_their_repository_and_other_hosts_do_not():
    # W416: the add-a-worker-host procedure clones through `Host github-<alias>`
    # (HostName github.com); such an origin names its GitHub repository.
    assert repository_name("github-applications:example-org/app-ecosystem.git") == "example-org/app-ecosystem"
    assert repository_name("git@github-applications:example-org/app-ecosystem.git") == "example-org/app-ecosystem"
    assert repository_name("ssh://git@github-applications/example-org/app-ecosystem.git") == "example-org/app-ecosystem"
    assert repository_name("ssh://git@github.com/example-org/app-ecosystem.git") == "example-org/app-ecosystem"
    # Anything else stays "not a GitHub repository".
    assert repository_name("gitlab-applications:example-org/app-ecosystem.git") == ""
    assert repository_name("git@gitlab.com:example-org/app-ecosystem.git") == ""
    assert repository_name("bob@github-applications:example-org/app-ecosystem.git") == ""
    assert repository_name("https://github-applications/example-org/app-ecosystem.git") == ""
    assert repository_name("ssh://git@example.test/example-org/app-ecosystem.git") == ""
    assert repository_name("github-:example-org/app-ecosystem.git") == ""
    assert repository_name("github-applications:example-org/a/b") == ""
    # gh picks the repository from an alias origin too.
    assert gh_repository(["pr", "list"], "git@github-app:example-org/z.git") == "example-org/z"


def test_gh_api_takes_its_repository_from_the_repo_flag_but_gh_never_sees_it():
    # W416: `gh api` has no -R/--repo and refuses one; the flag only selects the key.
    assert gh_forwarded_args(["api", "-R", "example-org/x", "repos/example-org/x/pulls"]) == ["api", "repos/example-org/x/pulls"]
    assert gh_forwarded_args(["api", "--repo", "example-org/x", "user"]) == ["api", "user"]
    assert gh_forwarded_args(["api", "--repo=example-org/x", "user"]) == ["api", "user"]
    # Every other gh command takes -R itself and keeps it.
    assert gh_forwarded_args(["pr", "create", "-R", "example-org/x"]) == ["pr", "create", "-R", "example-org/x"]
    assert gh_forwarded_args([]) == []


def test_pb_worker_gh_selects_the_key_from_an_alias_origin_and_strips_repo_for_api(monkeypatch):
    issued: list[str] = []

    class _Recording(_Session):
        def token(self, repository):
            issued.append(repository)
            return super().token(repository)

    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "_GitHubKeySession", _Recording)
    monkeypatch.setattr(cli, "find_gh", lambda **_: "gh")
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="git@github-app:example-org/app-ecosystem.git\n"),
    )
    monkeypatch.setattr(cli.subprocess, "call", lambda argv, env=None: calls.append(list(argv)) or 0)

    assert cli._gh_command(SimpleNamespace(gh_args=["--", "pr", "list"])) == 0  # noqa: SLF001
    assert cli._gh_command(  # noqa: SLF001
        SimpleNamespace(gh_args=["--", "api", "-R", "example-org/app-ecosystem", "repos/example-org/app-ecosystem"])
    ) == 0
    assert issued == ["example-org/app-ecosystem", "example-org/app-ecosystem"]
    assert calls == [["gh", "pr", "list"], ["gh", "api", "repos/example-org/app-ecosystem"]]


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


def test_a_card_login_that_cannot_refresh_is_named_not_a_traceback(monkeypatch, capsys):
    """2026-09-28 16:14Z: the helper crashed while refreshing the Card's own login."""

    from connection_hub.caller.errors import UpstreamError

    class _Failing(_Session):
        def token(self, repository):
            raise UpstreamError("oauth_token_request_failed", "The Card's login could not be refreshed: the token endpoint did not answer.")

    monkeypatch.setattr(cli, "_GitHubKeySession", _Failing)
    out = io.StringIO()
    code = cli._git_credential_command(  # noqa: SLF001
        SimpleNamespace(action="get"),
        stdin=io.StringIO("protocol=https\nhost=github.com\npath=example-org/app-ecosystem.git\n\n"),
        stdout=out,
    )
    err = capsys.readouterr().err
    assert code == 0 and out.getvalue() == ""
    assert "could not be refreshed" in err and "oauth_token_request_failed" in err
    assert "Traceback" not in err

    class _Unexpected(_Session):
        def token(self, repository):
            raise KeyError("ghu_should_not_print")

    monkeypatch.setattr(cli, "_GitHubKeySession", _Unexpected)
    cli._git_credential_command(  # noqa: SLF001
        SimpleNamespace(action="get"),
        stdin=io.StringIO("protocol=https\nhost=github.com\npath=example-org/app-ecosystem.git\n\n"),
        stdout=io.StringIO(),
    )
    err = capsys.readouterr().err
    assert "KeyError" in err and "ghu_should_not_print" not in err


def test_gh_is_found_without_the_service_path_and_named_when_missing(tmp_path):
    """W371 line 5, 2026-09-28 17:18Z: the launchd relay's PATH has no /opt/homebrew/bin."""

    import os

    from project_board.client.github_key import find_gh

    installed = tmp_path / "homebrew" / "bin" / "gh"
    installed.parent.mkdir(parents=True)
    installed.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(installed, 0o755)

    assert find_gh(path="", locations=(str(tmp_path / "nowhere" / "gh"), str(installed))) == str(installed)
    assert find_gh(path=str(installed.parent), locations=()) == str(installed), "PATH first"
    assert find_gh(path="", locations=(str(tmp_path / "nowhere" / "gh"),)) == ""


# --- the deploy-key fallback when the key is unavailable (Connection Hub down, 17:21Z) ---

from project_board.client.github_key import classify_failure, push_through_deploy_key  # noqa: E402


@pytest.mark.parametrize(
    "status, body, code, availability",
    [
        (502, {}, "http_502", True),
        (503, {}, "http_503", True),
        (200, {"ok": False, "error": "project_github_authorization_unavailable", "status": 503}, "project_github_authorization_unavailable", True),
        (200, {"ok": False, "error": "github_not_linked"}, "github_not_linked", False),
        (200, {"ok": False, "error": "card_denies"}, "card_denies", False),
    ],
)
def test_an_unavailable_key_is_told_apart_from_a_refusal(status, body, code, availability):
    with pytest.raises(GitHubKeyRefused) as refused:
        asyncio.run(issue_token(post=_post(status, body, []), url="u", bearer="b", project_ref="p", repository="o/r"))
    assert (refused.value.code, refused.value.availability) == (code, availability)
    assert classify_failure(refused.value) == (code, availability)


def test_transport_failures_are_availability_and_a_refused_card_is_not():
    from connection_hub.caller.errors import CredentialError, UpstreamError

    assert classify_failure(TimeoutError()) == ("connection_hub_unreachable", True)
    assert classify_failure(ConnectionRefusedError()) == ("connection_hub_unreachable", True)
    assert classify_failure(UpstreamError("oauth_token_request_failed", "no answer"))[1] is True
    assert classify_failure(CredentialError("credential_missing", "gone"))[1] is False


def test_the_same_push_is_aimed_at_the_deploy_key_remote():
    remotes = {"origin", "deploykey", "upstream"}
    assert push_through_deploy_key(["origin", "HEAD:refs/heads/x"], remotes) == ["deploykey", "HEAD:refs/heads/x"]
    assert push_through_deploy_key(["-u", "origin", "feature"], remotes) == ["-u", "deploykey", "feature"]
    assert push_through_deploy_key([], remotes) == ["deploykey"]
    assert push_through_deploy_key(["--force-with-lease"], remotes) == ["--force-with-lease", "deploykey"]
