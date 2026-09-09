# SPDX-License-Identifier: MIT
import ast
import pathlib

import pytest

from connection_hub.browser_session.cookies import StandardCookiePolicy

PACKAGE = pathlib.Path(__file__).resolve().parents[2] / "src" / "connection_hub" / "browser_session"
ALLOWED_THIRD_PARTY = {"httpx", "jwt"}


def test_default_policy_is_httponly_secure_lax_host_only():
    policy = StandardCookiePolicy(session_name="__Secure-LATC")
    session = policy.session_cookie("kst1.a.b", max_age=3600)
    assert (session.name, session.value, session.max_age) == ("__Secure-LATC", "kst1.a.b", 3600)
    assert session.http_only and session.secure and session.same_site == "lax" and session.path == "/" and session.domain == ""
    assert policy.clear_session_cookie().clears
    attempt = policy.attempt_cookie("bind", max_age=300)
    assert attempt.name == "__Host-kdcube-login" and attempt.domain == "" and attempt.http_only
    assert policy.clear_attempt_cookie().name == "__Host-kdcube-login"


def test_a_domain_or_insecure_setting_drops_the_host_prefix():
    policy = StandardCookiePolicy(session_name="s", domain=".example.test")
    assert policy.attempt_name == "kdcube-login"
    assert policy.session_cookie("t", max_age=10).domain == ".example.test"
    assert policy.attempt_cookie("b", max_age=10).domain == ".example.test"
    insecure = StandardCookiePolicy(session_name="s", secure=False)
    assert insecure.attempt_name == "kdcube-login" and insecure.session_cookie("t", max_age=1).secure is False
    with pytest.raises(ValueError):
        StandardCookiePolicy(session_name="s", same_site="weird")
    with pytest.raises(ValueError):
        StandardCookiePolicy(session_name="")


def test_browser_session_imports_nothing_from_a_platform_or_the_rest_of_the_hub():
    """The subpackage must stay liftable into a foundation package: standard
    library, its own modules, and at most httpx and PyJWT (guarded)."""
    offenders = []
    for path in PACKAGE.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                root = name.split(".")[0]
                if name.startswith("connection_hub.browser_session"):
                    continue
                if root == "connection_hub":
                    offenders.append((path.name, name))
                elif root == "kdcube_ai_app" or (root in ALLOWED_THIRD_PARTY) is False and not _stdlib(root):
                    offenders.append((path.name, name))
    assert offenders == []


def _stdlib(root: str) -> bool:
    import sys
    return root in sys.stdlib_module_names
