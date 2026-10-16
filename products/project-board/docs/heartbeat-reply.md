---
id: project-board.heartbeat-reply
title: Lossless conditional heartbeat replies
summary: Stateless section and observation-group negotiation, client reconstruction, and full recovery without retaining authority.
tags: [heartbeat, protocol, display, performance]
---

# Lossless conditional heartbeat replies

`worker.heartbeat` can return only the sections and observation groups that
changed, while the relay consumes the same complete result as before. The Board
still performs fresh admission, records observations, reads state, and builds the
complete result on every call. This reduces repeated wire data, not authority
reads or server response construction.

The shared implementation is `project_board.contract.heartbeat_reply`. The
Board supplies the admitted scope; `project_board.client.heartbeat_reply` owns
the relay's process-memory snapshot pairs. There is no server-side client cache,
new durable store, generation counter, cursor or cross-request barrier.

## Negotiation and wire shape

A request opts in with `reply_protocol: problem_board.heartbeat_reply.v1`.
The relay may send `known_reply` only alongside its intact locally retained
snapshot:

```json
{
  "scope": "<64 lowercase hex scope digest>",
  "revision": "<64 lowercase hex complete-snapshot digest>",
  "sections": {
    "team": {"b": "<22-character body identity>", "o": {"0": "<22-character group identity>"}}
  }
}
```

The echo contains identities, not prior values or arbitrary client-selected
omission instructions. Its compact canonical size is limited to 32,768 bytes.
An absent, malformed, oversized or foreign-scope echo selects a complete reply.

The response has one `heartbeat_reply` marker. Its `protocol`, `scope` and
`revision` accompany either:

- `state: full` and the complete `snapshot`; or
- `state: delta`, the exact request's `base` revision, and optional `set`,
  `observations`, and `remove` fields. An identical result needs only the marker.

`set` replaces whole top-level sections, including newly added sections.
`remove` explicitly lists removed top-level sections. A real `team: []` in
`set` clears TEAM; an omitted `team` means retain and reconstruct, not clear.
Changed assignment bodies are complete, even when large. No list is truncated
to satisfy a byte budget.

An unchanged section body may carry changed **whole observation groups**, not
changed-leaf tuples. Each TEAM row is one group, indexed by its list position;
each other section has one group named `""`. The group maps escaped JSON Pointer
paths to exact primitive values. Observation keys are `observed_at`,
`heartbeat_at`, `updated_at`, `store_reads_observed_at`, `host_free_bytes`,
`reported_at`, `runtime_account_observed_at`, `last_inbox_check_at`,
`relay_heartbeat_at`, `heartbeat_age_seconds`, `relay_heartbeat_age_seconds`,
and `at` underneath `store_reads` (including when `store_reads` is itself the
top-level section). In normalized store-read entries, primitive `ms` and `op`
are observations only at `store_reads.<store>`, `self.store_reads.<store>` and
`team[i].store_reads.<store>`. The TEAM classifier starts at each row. The same
names at other paths, or structured dict/list values, stay in the body. Existing
timestamp classification is unchanged. This includes the Board's per-heartbeat
disk report time and projected session ages. Other fields remain part of the
body, including unknown future fields, usage windows/resets, semantic session
state, workspace size, and store-read counts, keys and ranges. No value is
removed, rounded, suppressed or coarsened.

The body identity binds both its values and its exact observation layout.
Reordering/removing rows, adding/removing observation paths, or changing stable
values replaces the whole section. Absent, null, empty string, object and list
remain distinct. Observation-path indices are reused only with that same body
and layout. A timestamp change inside a group sends the entire current group.
Old and new classifiers have different body/layout identities for summaries
with `ms`/`op`, so mixed versions replace the complete section in either
direction using the existing protocol; no new negotiation or layout is added.

## Identity and trust

Canonical JSON uses sorted object keys, ASCII escaping, compact separators and
finite numbers. Complete revisions and scopes use SHA-256. Body and observation
identities are the first 128 bits of SHA-256, URL-safe Base64 without padding.
Golden vectors run in both the contract and admitted Board consumer suites;
neither peer maintains a separate canonicalization algorithm.

The server scope includes tenant, platform project, bundle, admitted viewer,
authority identity, project reference (empty for discovery), view, filters and
protocol. It is not chosen by the requester. Content identities are not
monotonic revisions, action authority, admission receipts or resumable cursors.
A → B → A can reuse A's identity. A forged echo cannot authorize anything or
skip a Card check: encoding occurs after the fresh complete response is built.

## Reconstruction and recovery

The client checks the exact baseline revision and scope, applies complete
sections and observation groups to a defensive copy, then verifies the
complete result's SHA-256 **before** any consumer sees it. Conflicting edits,
unknown removed sections, invalid observation layout, missing baseline or a
digest mismatch discard knowledge and require a full next reply. This is a
retryable display recovery, not an authorization refusal.

One snapshot owner is shared across rebuilt attendance adapters. Per-project
request sequences and the public Data Bus connection generation fence late
responses. The context also binds client object, worker/viewer, configured
profile and partition. A restart, disconnect, unknown generation, replaced
client, changed context, unlink, corrupt snapshot or failed materialization
removes the corresponding conditional knowledge. Republish drops project
knowledge and retries discovery without an echo. No bare digest is persisted.

The relay reconstructs discovery as well as project heartbeats. Existing
attendance, TEAM, coordinator, roles, journal, mailbox-directory and assignment
consumers receive full values. Field callbacks, subsequent consumer failures
and explicit reconciliation issues discard knowledge so that the next reply
is full. Defensive copies isolate retained values from consumer mutation.

Requests without this protocol retain legacy replies. The older
[TEAM-only protocol](heartbeat-team.md) remains supported; complete-reply
negotiation takes precedence when both are supplied. A new client receiving an
old or rolled-back Board's full legacy reply materializes it and clears its
conditional knowledge. Unsupported requests or an unhashable server result
fall back to legacy full replies. The Board and shared package are installed
as a tested pair; independently installed worker clients may be old or new.

## Verification and limits

The contract suite covers exact reconstruction, every scope dimension,
observation layout, unknown/removed sections, large assignments, malformed and
oversized knowledge, and mixed versions. Relay tests exercise real preparation,
project polling, discovery, republish, materialization and reconnect paths.
Admitted Board dispatch tests prove fresh authorization even with a matching
echo and verify the same canonical golden vectors.

Synthetic wire examples do not prove live savings. Qualification records both
request and reply bytes (with the real SDK envelope), full-versus-delta rates,
and matched-workload per-heartbeat time and ingress/proc CPU after release.
Encoding, hashing, validation and reconstruction add CPU; no reduction in
backend reads or build cost is claimed. Cold start, recovery and genuine
section changes may legitimately send large full values. Heartbeat cadence,
control delivery, wake retry and Data Bus transport are unchanged by this
protocol.

Classification uses the same existing body/layout identities and recovery
contract as before. Qualification includes the Board's actual response paths,
not only a hand-written subset of TEAM fields. The original real-shaped
13-member test preserves whole-row groups under a 7 KB synthetic-frame budget.
Broader normalized store-read churn is tested separately without treating that
smaller fixture as a universal size bound; actual live SDK-envelope size remains
a separate measurement.
