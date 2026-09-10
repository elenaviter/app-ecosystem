# SPDX-License-Identifier: MIT
import pytest

from connection_hub.browser_session.next_url import safe_next_path


@pytest.mark.parametrize("raw", ["/", "/platform/chat", "/sites/connections/?tab=cards#top", "/a b/c"])
def test_same_origin_paths_pass(raw):
    assert safe_next_path(raw) == raw


@pytest.mark.parametrize(
    "raw",
    [
        "", None, "platform/chat", "https://evil.test/", "//evil.test/x", "/\\evil.test", "/x\\y",
        "javascript:alert(1)", "/ok\nSet-Cookie: x=y",
    ],
)
def test_everything_else_falls_back(raw):
    assert safe_next_path(raw) == "/"
    assert safe_next_path(raw, default="/home") == "/home"


def test_surrounding_whitespace_is_trimmed_like_a_browser_does():
    assert safe_next_path("\t/leading-tab ") == "/leading-tab"


import pytest as _pytest
from connection_hub.browser_session.next_url import safe_next_target


@_pytest.mark.parametrize(
    "raw, allowed, expected",
    [
        ("/chat?x=1#t", (), "/chat?x=1#t"),
        ("https://www.kdcube.example/page?q=1#h", ("https://www.kdcube.example",), "https://www.kdcube.example/page?q=1#h"),
        ("https://WWW.kdcube.example/page", ("https://www.kdcube.example",), "https://www.kdcube.example/page"),
        ("https://pr22.kdcube.example/x", ("https://*.kdcube.example",), "https://pr22.kdcube.example/x"),
        ("https://kdcube.example/x", ("https://*.kdcube.example",), "/"),
        ("https://a.b.kdcube.example/x", ("https://*.kdcube.example",), "/"),
        ("http://www.kdcube.example/x", ("https://www.kdcube.example",), "/"),
        ("https://evil.example/x", ("https://www.kdcube.example",), "/"),
        ("https://www.kdcube.example.evil.example/x", ("https://www.kdcube.example",), "/"),
        ("https://user:pw@www.kdcube.example/x", ("https://www.kdcube.example",), "/"),
        ("https://www.kdcube.example//x", ("https://www.kdcube.example",), "/"),
        ("https://www.kdcube.example", ("https://www.kdcube.example",), "https://www.kdcube.example/"),
        ("javascript:alert(1)", ("https://www.kdcube.example",), "/"),
        ("//evil.example", ("https://www.kdcube.example",), "/"),
        ("", ("https://www.kdcube.example",), "/"),
    ],
)
def test_safe_next_target_returns_only_to_listed_origins(raw, allowed, expected):
    assert safe_next_target(raw, allowed_origins=allowed) == expected


def test_flow_begin_login_returns_to_a_listed_origin():
    import asyncio
    from connection_hub.browser_session.cookies import StandardCookiePolicy
    from connection_hub.browser_session.flow import BrowserSessionFlow
    from connection_hub.browser_session.memory import MemoryLoginAttemptStore, MemorySessionBackend
    from connection_hub.browser_session.model import LoginAttempt, SessionPolicy, VerifiedIdentity

    class Upstream:
        name = "u"
        async def begin(self, attempt: LoginAttempt) -> str: return "https://idp.example/a"
        async def complete(self, params, attempt): return VerifiedIdentity(provider="u", subject="s")
        def logout_url(self, *, post_logout_redirect: str = "") -> str: return ""

    flow = BrowserSessionFlow(
        backend=MemorySessionBackend(secret="s"), attempts=MemoryLoginAttemptStore(), upstream=Upstream(),
        cookies=StandardCookiePolicy(), policy=SessionPolicy(return_origins=("https://www.kdcube.example",)),
    )
    start = asyncio.run(flow.begin_login("https://www.kdcube.example/docs?x=1"))
    assert start.attempt.next_path == "https://www.kdcube.example/docs?x=1"
    assert asyncio.run(flow.begin_login("https://evil.example/")).attempt.next_path == "/"
