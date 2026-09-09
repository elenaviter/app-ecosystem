---
id: docs/connection-hub/package/browser-session
title: "Browser Session: one server-held session for every surface"
summary: "The host-neutral browser session in the connection-hub package: one HttpOnly cookie, server-held upstream identity, sliding renewal, an OIDC code-flow upstream and a Google Identity Services upstream, and the protocols a host implements."
status: "active"
tags: ["connection-hub", "package", "browser-session", "oidc", "cognito", "google", "session", "bff"]
updated_at: 2026-09-09
see_also:
  - "./delegated-authority-and-admission.md"
  - "./oauth-delegated-credential-protocol.md"
  - "https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/service/auth/app-hosted-platform-login-and-session-README.md"
---

# Browser Session

`connection_hub.browser_session` is the server-held browser session, the
pattern often called a backend-for-frontend login. The browser holds one
HttpOnly, Secure, SameSite=Lax cookie with a signed session token. The server
holds the upstream identity, renews nothing in the browser, and every surface
of a platform (a control plane, a widget, an application-served site) asks one
profile endpoint whether it is signed in. Sign-out in one tab signs out all,
because there is one cookie.

## Why

Three browser copies of "get an identity token into a cookie" (a chat
frontend, a website, a widget) each ran an OIDC client in JavaScript, wrote
bearer tokens into cookies any same-origin script could read, and renewed
only while one particular tab was open. Sharing a refresh token across tabs
extends a long-lived credential to every script on the origin; iframe silent
renewal depends on third-party cookies browsers are retiring. A server-held
session needs neither: the cookie is unreadable to scripts, the server slides
the session on activity, and a site needs only `/profile` and a sign-in
redirect.

## What it is

Host-neutral by construction: the subpackage imports nothing from the rest of
Connection Hub and nothing from any platform, a test pins that boundary.
Everything a host owns is injected through protocols:

| Protocol | The host supplies |
| --- | --- |
| `SessionBackend` | `login_or_register(identity, expires_at)`, `validate(token, now)`, `touch(session_id, expires_at, now)`, `logout(token)`: sessions and the platform user record behind them (KDCube: its Redis session registry and user records). |
| `LoginAttemptStore` | `put(attempt)`, `take(state)`: one-time login attempts; `take` returns an attempt at most once. |
| `UpstreamIdentity` | `begin(attempt) -> redirect url`, `complete(params, attempt) -> VerifiedIdentity`, `logout_url()`: who proves the identity. |
| `CookiePolicy` | Names and attributes of the session and attempt cookies, from deployment configuration. `StandardCookiePolicy` is the default. |
| `IdTokenVerifier` | Signature, issuer, audience and expiry of an upstream ID token. `oidc_jwt.PyJwtVerifier` when PyJWT is installed (`pip install connection-hub[oidc]`). |

`BrowserSessionFlow` is the flow itself, four pure methods a host's router
mounts:

```text
begin_login(next)                      -> LoginRedirect: where to send the browser, the attempt cookie
complete_login(params, attempt_binding) -> LoginCompleted: the session cookie, where to redirect
validate_request(token)                -> SessionState, slid forward when due, or None
logout(token)                          -> LogoutCompleted: the cleared cookie, an upstream logout URL
```

## The login attempt

`begin_login` stores a one-time attempt under a random `state` (with a PKCE
verifier and a nonce) and hands the browser a second random secret, the
`binding`, in a short-lived HttpOnly cookie (`__Host-kdcube-login` by
default). `complete_login` takes the attempt out of the store, so an
authorization code cannot be replayed, and requires the binding cookie to
match, so only the browser that started the sign-in can finish it. The
destination goes through `safe_next_path` when the login starts: a same-origin
absolute path or the default, never a host, a scheme, `//`, or a backslash.
The callback never chooses where the browser goes.

## Sliding renewal

`SessionPolicy(idle_ttl_seconds=12h, max_ttl_seconds=7d, touch_interval_seconds=60)`.
A validated request whose last extension is older than the touch interval
moves the session's expiry to the idle limit from now, never past the maximum
since sign-in. The cookie's max-age is the maximum, so the browser keeps the
cookie while the server decides. Nothing in the browser renews anything.

## Upstreams

- `oidc.OidcCodeFlow`: the OIDC authorization-code flow with PKCE for any
  issuer. `OidcClientConfig.cognito(...)` presets a Cognito user pool: the
  hosted UI's `/logout` with `logout_uri` as the post-logout parameter.
  Discovery, the code exchange and ID-token verification are injectable;
  the defaults use `httpx` and the injected verifier. The `nonce` claim must
  equal the attempt's nonce.
- `google_identity.GoogleIdentityUpstream`: the Google Identity Services
  credential posted to the host's login page, verified against Google's
  issuers and the web client id, bound to the attempt by nonce. This is the
  port of the platform's reference Google login with its three hardening
  gaps closed by construction (one-time bound attempt, nonce, destination
  guard).

## The token

`kst1.<base64url claims>.<base64url HMAC-SHA256>` with sorted compact JSON
claims, the platform's own session token, byte for byte. A host with its own
session registry keeps minting tokens as it does; `memory.MemorySessionBackend`
shows a complete backend for tests and single-process hosts.

## What a host mounts

The host's router translates the flow into HTTP: a login route that calls
`begin_login(next)` and answers a redirect with the attempt cookie set, a
callback route that calls `complete_login` with the query parameters and the
attempt cookie and answers a redirect with the session cookie set and the
attempt cookie cleared, a logout route, and the request validation in the
gateway with the sliding touch. The KDCube platform mounts them under
`/api/platform/session/*`; see the platform's session README.
