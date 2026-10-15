---
id: project-board.heartbeat-team
title: Conditional heartbeat TEAM snapshots
summary: The paired v1 display contract, content identity, local retention and full-snapshot recovery.
tags: [heartbeat, team, protocol, display]
---

# Conditional heartbeat TEAM snapshots

This remains the older TEAM-only negotiation. New relays use the
[complete-reply protocol](heartbeat-reply.md), which takes precedence when both
are requested; the TEAM-only behavior below remains supported for older peers.

The optional `worker.heartbeat` TEAM protocol reduces repeated display payloads,
not authorization reads. The server builds the complete TEAM afresh on every
heartbeat, after recording the reporting worker's current observations. Current
Card, parent Control, grants, action eligibility, attendance and mailbox admission
are never taken from a retained TEAM or its revision.

## Wire contract

An opted-in request supplies `team_protocol: problem_board.heartbeat_team.v1`.
It may also supply `known_team_revision` only when the client retains the complete
successfully materialized snapshot associated with that exact revision.

The response contains `team_view`:

```json
{
  "protocol": "problem_board.heartbeat_team.v1",
  "state": "changed",
  "revision": "team-v1:<64 lowercase hex scope digest>:<64 lowercase hex content digest>",
  "scope": "<64 lowercase hex scope digest>"
}
```

| State | `team` field | Client behavior |
| --- | --- | --- |
| `changed` | Complete current list, including a real empty list | Replace the whole display; retain the pair only after materialization succeeds. |
| `unchanged` | Omitted, never `[]` or `null` | Use exactly the intact snapshot whose revision this request supplied. |
| Legacy or unsupported request | Complete list, no negotiated marker | Replace the whole display; retain no conditional knowledge. |

Malformed, oversized, foreign-scope or unknown known revisions get a complete
snapshot, not a heartbeat failure. An unhashable display or incomplete server
scope falls back to the full legacy response. Other heartbeat fields are not
conditional: own sessions, attendance, coordinator, roles, assignments, project
permissions and compact `mail_recipients` remain current.

The scope digest is derived by the server from its tenant, platform project,
bundle, admitted viewer principal and authority identity, project reference,
`heartbeat.team` view, `all-linked` filter and protocol. It carries no raw
identity or credential. The client cannot select the server's scope.

The content digest is SHA-256 of the entire TEAM serialized with sorted object
keys, ASCII JSON escaping, compact separators and finite JSON numbers. List
order, every field, absent-versus-empty values and every observation/provenance
or wake field are preserved. No timestamp is removed or coarsened to obtain
unchanged hits. Shared helpers live in `project_board.contract.heartbeat_team`;
the Board owns admitted scope construction and fresh response selection.

This is a version-prefixed opaque content identity, not a SQL generation,
monotonic revision, authority lease or resumable cursor. A → B → A may reuse A's
identity. A raw parent Control revision that leaves complete TEAM unchanged does
not invalidate it; a Control change visible in TEAM does. Authorization is fresh
on every call, independent of whether a TEAM body is sent.

## Local retention and recovery

`project_board.client.heartbeat_team.TeamSnapshots` owns process-memory-only
snapshot/revision pairs. The channel's rebuilt attendance adapters share that
owner. A restart begins without any conditional knowledge; a revision is never
persisted separately from its snapshot.

Materialization precedes revision advancement. A local request sequence and the
public Data Bus connection generation fence late replies. The fence also includes
client-object identity, worker/viewer identity, configured profile and partition.
A changed full body is checked against its content digest before materialization.
Consumer mutation cannot corrupt a retained pair because its copies are separate.

Cache loss/corruption, project unlink, account/profile/viewer/channel changes,
unknown transport generation, disconnected transport, failed materialization or
heartbeat refusal require a full next response. A reconnect in the same client
changes its public connection generation; replacing the client also invalidates
the pair. An unchanged marker without the exact intact pair, or one accompanied
by a `team` body, is not success or an implicit clear: it is ignored and conditional
knowledge is dropped. A real changed `team: []` clears the roster.

Old clients receive complete teams from a new server. New clients receiving an
old/rolled-back server's complete legacy response replace the display and clear
conditional knowledge. Unknown protocols likewise recover through full snapshots.
The server bundle and shared contract package must be released as a tested pair;
the independently installed worker client may be old or new during that window.

## Verification and performance limits

Focused server tests cover whole-content identity, scope dimensions, observation
changes, order/removal/empty distinctions and the actual admitted dispatch. The
dispatch regression is runnable unchanged on the preceding bundle and fails its
protocol assertion there. The fresh Card guard still refuses a matching display
revision after the operation is revoked. Client tests cover retention, failed
materialization, reconnect/late-response fencing, empty replacements, mixed
versions and the actual relay materializer/public transport-generation wrapper.

Wire savings depend on actual unchanged-hit rate. Observation updates may prevent
hits. Hashing and client integrity checks add work; server TEAM reads/builds do not
decrease. Synthetic stable snapshots can demonstrate byte differences, but do not
establish live throughput or CPU savings. Qualification must name the paired heads,
unchanged-hit ratio, matched workload bytes and whole-process ingress/proc CPU.
Independent qualification and a coordinated paired release remain distinct from
source readiness.
