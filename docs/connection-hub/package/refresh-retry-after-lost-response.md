---
id: connection-hub/package/refresh-retry-after-lost-response
title: "Refresh Retry After A Lost Token Response"
summary: "A proposed protocol that lets a client retry one refresh whose response was lost after the server rotated, without weakening refresh-token reuse detection."
status: proposed
tags: ["connection-hub", "oauth", "refresh", "rotation", "security", "proposal"]
keywords: ["refresh token rotation", "reuse detection", "lost response", "ReadError", "refresh attempt", "credential family", "W408"]
updated_at: 2026-09-30
see_also:
  - ./durable-authority-generations.md
  - ./oauth-delegated-credential-protocol.md
---

# Refresh Retry After A Lost Token Response

Status: proposed for independent security review (W408). Nothing here is active.

## Why this exists

Connection Hub rotates a refresh token on every use and treats a second use of a consumed token as theft. It revokes the whole credential family, so the Card's holder must authorize again. That rule is right when two parties hold the same token. It also fires when one honest client loses the token response.

On 2026-09-30 a worker's relay sent a refresh. The server committed the rotation, and the client's read of the response failed (`ReadError`). The client kept the token it had sent, as it must, because no new token arrived. The relay reopened the channel 105 s later with that token. Reuse detection revoked the family, and every later renewal was refused until a person reauthorized the worker.

The test `products/connection-hub/packages/connection-hub/tests/test_w408_lost_refresh_response_postgres.py` reproduces this with the real client and the real PostgreSQL authority store.

## What each failure leaves behind

| Failure during a refresh | Server state | Client holds | Next refresh today |
|---|---|---|---|
| Connect failure before send | unchanged | the live token | succeeds |
| Task cancelled before send | unchanged | the live token | succeeds |
| Read failure after send (the live case) | rotated | the consumed token | revokes the family |
| Task cancelled after the server committed | rotated | the consumed token | revokes the family |
| Response delivered, native-store write failed | rotated | the consumed token | revokes the family |

The last three are one case: the server rotated, and the new token never became the client's stored credential. No client-side change can recover a token that never arrived. The server has to recognise a retry of the same refresh.

## Threat model

Rotation with reuse detection defends against one party using a refresh token that another party also holds (a copied credential store, a leaked token, a token read from a backup or a log). When both use it, the second use is detected and the family ends.

A retry rule must not give that party a way to keep a family alive. It may only help the client that sent the lost request. So a retry is accepted only with evidence that the same client is retrying the same request, and only while nobody has used the rotation it produced.

Out of scope: an attacker who holds the whole native credential store. They already hold the current token and can refresh normally, and no refresh rule can tell them from the client.

## Alternatives considered

1. **Grace window for any re-presentation.** Accept the just-consumed token for N seconds and rotate again. Rejected: anyone holding the old token gets a fresh one inside the window. This is broad replay.
2. **Return the same successor on retry.** Rejected: the server would have to store a recoverable successor bearer. Today it stores only hashes, and this would be a new custody model.
3. **Client never retries after an unknown outcome.** The client reports "renewal outcome unknown" and waits for a person. This avoids the revocation, but the worker is stranded exactly as before, because its token is consumed. Kept only as the fallback when the retry below is refused.
4. **Longer access tokens or fewer rotations.** This reduces how often the race can happen. It does not remove it.
5. **Attempt-bound single retry (proposed).** Described below.

## Proposed protocol

**Client.** Before sending a refresh, the client creates a random attempt id (32 bytes, URL-safe). It stores the id with the token in the native credential store before the request leaves. When the store cannot take the id, the refresh is not sent at all: the token is still live, and a refresh sent without a stored id could not be retried if its response were lost. The client sends the id as the form field `refresh_attempt`. A successful response replaces the token and clears the id, and so does a refusal with a server status, since nothing rotated. On a post-send unknown outcome (read failure, a timeout after send, cancellation after send, a failed store write), the id stays with the token, and every later refresh of that token sends the same id. A connect failure before send needs no special handling, because the token is still live.

**Server, on rotation.** The new generation records its parent generation and a fingerprint of the request that minted it:
`sha256(attempt id, client id, resource, requested scope)`. The attempt id itself is never stored or logged.

**Server, on a consumed token.** A consumed generation presented again is a retry of the same refresh, not reuse, only when every check holds in one transaction, under the same row locks as rotation:

- the request carries an attempt id, and its fingerprint equals the one on the family's current generation.
- that current generation's parent is the presented generation.
- the current generation has never been used, and fewer than 5 retries of this attempt minted it (`MAX_REFRESH_RETRIES`).
- the family is active and unexpired, and the presented generation was consumed within the retry window.
- the Card behind the family is still live, and its client, resource and scope checks pass as for any refresh.

On a retry, the unused successor is revoked and a new successor is minted from the presented generation, carrying the retry count. Repeated post-send losses (a flaky tunnel) are therefore retried again, up to the cap. The window runs from the presented generation's first consumption, so retries never extend it. Anything else, including a sixth retry of the same attempt, keeps today's rule: the family is revoked.

**Compatibility.** A server without this protocol ignores `refresh_attempt`. A client without it sends none, and the server applies today's rule. W291 post-rotation issuance compensation is unchanged: rolling back a retried rotation restores the presented generation exactly as it does after an ordinary rotation.

## The retry window

The window bounds how long after the lost response a retry is recognised. It has to cover the client's real retry schedule. The relay's channel backoff starts at 60 s and doubles to a cap of 1800 s (`relay_pacing.CHANNEL_BACKOFF_BASE_SECONDS` and `CHANNEL_BACKOFF_CAP_SECONDS`), and the live retry came 105 s after the loss. A retry that fails before send keeps the attempt pending for the next backoff step.

The relay's channel backoff retries at 60, 180, 420 and 900 s after the failure (cumulative, doubling from 60 s), and each retry can wait up to one 60 s relay cycle more. The proposal is 1200 s: it covers the fourth retry at 900 s plus four cycles (1140 s), with slack, and the tests check retries at 900 and 1140 s inside the window and one just past it outside. A longer window adds little risk, because a retry already needs the attempt id, an unused successor, the same client, resource and scope, and at most 5 retries anchored at the first consumption. It is a server constant (`REFRESH_RETRY_WINDOW_SECONDS`). The value is an explicit policy decision for review.

## Concurrency and fences

- **Concurrent retries.** On one host the profile's refresh lock allows one refresh at a time, so a client never sends two retries of one attempt together. A duplicated delivery of one retry is serialised by the generation row locks: both are retries, the family stays active, and the later successor is the one live token. A client that stored the earlier response then holds a revoked token, and its next refresh is refused as unknown without revoking the family. This residual needs a duplicated POST, which the client never sends itself.
- **A late original response.** The client never stores a response that arrives after a failure was reported for that call, because the transport call has already ended. If a retry succeeded, its token is the stored one.
- **A late store write.** The client commits a refresh result only when the stored token is still the one it refreshed, which is the existing compare in `profile_session._commit_refreshed_token`. A stale result is dropped, never written over a newer token.
- **Revoked, expired or unrelated tokens.** No retry path applies. They are refused exactly as today.

## What review is asked to decide

1. Whether the attempt-bound retry (at most 5 per attempt) is acceptable, against alternatives 1 to 4.
2. The retry window (proposed 1200 s), as an explicit policy decision.
3. Whether a refused retry should revoke the family (proposed: yes, as today) or only refuse the request.
