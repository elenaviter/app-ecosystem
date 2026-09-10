# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""The cookie policy: names and attributes from deployment configuration.

Two cookies exist. The session cookie carries the signed session token for
the life of the session, HttpOnly, Secure, SameSite=Lax, Path=/, host-only
unless a same-site subdomain topology needs a ``Domain``. The attempt cookie
carries the login attempt's browser binding for the minutes a sign-in takes;
it uses the ``__Host-`` prefix when no domain is set, which browsers accept
only over HTTPS, host-only, at path ``/``.
"""

from __future__ import annotations

from dataclasses import dataclass

from connection_hub.browser_session.model import CookieSpec

DEFAULT_SESSION_COOKIE = "__Secure-LATC"
DEFAULT_ATTEMPT_COOKIE = "__Host-kdcube-login"
# Carries the destination across the identity provider's sign-out round trip,
# so the registered post-logout URL is one fixed route per origin and the
# browser still lands where it was.
DEFAULT_RETURN_COOKIE = "__Host-kdcube-return"
_ALLOWED_SAME_SITE = ("lax", "strict", "none")


@dataclass(frozen=True)
class StandardCookiePolicy:
    """``CookiePolicy`` with the platform's default attributes."""

    session_name: str = DEFAULT_SESSION_COOKIE
    attempt_name: str = DEFAULT_ATTEMPT_COOKIE
    return_name: str = DEFAULT_RETURN_COOKIE
    secure: bool = True
    same_site: str = "lax"
    domain: str = ""
    path: str = "/"

    def __post_init__(self) -> None:
        same_site = str(self.same_site or "lax").lower()
        if same_site not in _ALLOWED_SAME_SITE:
            raise ValueError(f"same_site must be one of {_ALLOWED_SAME_SITE}")
        object.__setattr__(self, "same_site", same_site)
        if not str(self.session_name or "").strip():
            raise ValueError("session cookie name is required")
        attempt_name = str(self.attempt_name or "").strip() or DEFAULT_ATTEMPT_COOKIE
        # A __Host- cookie may carry no Domain and needs Secure and Path=/.
        if attempt_name.startswith("__Host-") and (self.domain or not self.secure or self.path != "/"):
            attempt_name = "kdcube-login"
        object.__setattr__(self, "attempt_name", attempt_name)
        return_name = str(self.return_name or "").strip() or DEFAULT_RETURN_COOKIE
        if return_name.startswith("__Host-") and (self.domain or not self.secure or self.path != "/"):
            return_name = "kdcube-return"
        object.__setattr__(self, "return_name", return_name)

    def _spec(self, name: str, value: str, max_age: int, *, host_only: bool = False) -> CookieSpec:
        return CookieSpec(
            name=name,
            value=value,
            max_age=max_age,
            secure=self.secure,
            http_only=True,
            same_site=self.same_site,
            path=self.path,
            domain="" if host_only else self.domain,
        )

    def session_cookie(self, token: str, *, max_age: int) -> CookieSpec:
        return self._spec(self.session_name, token, max(1, int(max_age)))

    def clear_session_cookie(self) -> CookieSpec:
        return self._spec(self.session_name, "", 0)

    def attempt_cookie(self, binding: str, *, max_age: int) -> CookieSpec:
        return self._spec(self.attempt_name, binding, max(1, int(max_age)), host_only=self.attempt_name.startswith("__Host-"))

    def clear_attempt_cookie(self) -> CookieSpec:
        return self._spec(self.attempt_name, "", 0, host_only=self.attempt_name.startswith("__Host-"))

    def return_cookie(self, next_path: str, *, max_age: int) -> CookieSpec:
        """The destination to resume after the identity provider's sign-out."""
        return self._spec(self.return_name, next_path, max(1, int(max_age)), host_only=self.return_name.startswith("__Host-"))

    def clear_return_cookie(self) -> CookieSpec:
        return self._spec(self.return_name, "", 0, host_only=self.return_name.startswith("__Host-"))


__all__ = ["DEFAULT_ATTEMPT_COOKIE", "DEFAULT_RETURN_COOKIE", "DEFAULT_SESSION_COOKIE", "StandardCookiePolicy"]
