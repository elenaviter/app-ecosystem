# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""One server-held browser session for every surface of a platform.

The browser holds one HttpOnly cookie with a signed session token. The server
holds the upstream identity, renews nothing in the browser, and every surface
(a control plane, a widget, an application-served site) asks one profile
endpoint whether it is signed in. A session slides: each request extends it
by the idle limit, never past the maximum since sign-in.

This subpackage is host-neutral by construction. It imports nothing from the
rest of Connection Hub and nothing from any platform. Everything a host owns
is injected through small protocols (``protocols``): where sessions and login
attempts are stored, how the platform user is registered, what the cookies
are called, and which upstream proves the identity. The host mounts the
``BrowserSessionFlow`` under its own HTTP framework; the flow itself is pure
methods returning what to redirect, set, clear, and answer.

Modules:

- ``model``: the data types (login attempt, verified identity, issued session,
  session state, cookie specs, the sliding policy).
- ``protocols``: the injection points.
- ``flow``: ``BrowserSessionFlow``: begin_login, complete_login, logout,
  validate_request with sliding renewal.
- ``next_url``: the same-origin guard for the post-login destination.
- ``kst1``: the ``kst1.<body>.<signature>`` token codec, byte-compatible with
  the platform session token it replaces.
- ``oidc``: the OIDC authorization-code upstream (any issuer: Cognito, a
  generic OIDC provider), with PKCE, state, nonce, and injected ID-token
  verification; ``oidc_jwt`` supplies a verifier when PyJWT is installed.
- ``google_identity``: the Google Identity Services credential upstream.
- ``memory``: in-memory stores and a registry for tests and simple hosts.
"""

from connection_hub.browser_session.flow import (
    BrowserSessionFlow,
    LoginRedirect,
    LoginCompleted,
    LogoutCompleted,
    LoginAttemptRejected,
    LoginRejected,
)
from connection_hub.browser_session.model import (
    CookieSpec,
    IssuedSession,
    LoginAttempt,
    SessionPolicy,
    SessionState,
    VerifiedIdentity,
)
from connection_hub.browser_session.next_url import safe_next_path
from connection_hub.browser_session.protocols import (
    CookiePolicy,
    IdTokenVerifier,
    LoginAttemptStore,
    SessionBackend,
    UpstreamIdentity,
)

__all__ = [
    "BrowserSessionFlow",
    "CookiePolicy",
    "CookieSpec",
    "IdTokenVerifier",
    "IssuedSession",
    "LoginAttempt",
    "LoginAttemptRejected",
    "LoginAttemptStore",
    "LoginCompleted",
    "LoginRedirect",
    "LoginRejected",
    "LogoutCompleted",
    "SessionBackend",
    "SessionPolicy",
    "SessionState",
    "UpstreamIdentity",
    "VerifiedIdentity",
    "safe_next_path",
]
