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
