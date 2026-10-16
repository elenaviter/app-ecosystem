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

The Board's heartbeat-specific TEAM view omits other members' store-read and
disk-usage summaries, which the relay's local TEAM materializer already drops.
The reporter's own TEAM row, self/root values and all members' scheduling,
model/effort, account/provenance, usage/limit/reset and wake evidence remain.
Stored worker data, the common TEAM producer, attendance notices and the
Board/widget worker APIs retain their read/disk diagnostics. Complete replies
and reconstruction are lossless for this declared heartbeat view, not a copy
of the separate Board UI worker view. View changes replace complete sections
under the existing body/layout contract; there is no row-level replacement.

The shared implementation is `project_board.contract.heartbeat_reply`. The
Board supplies the admitted scope; `project_board.client.heartbeat_reply` owns
the relay's process-memory snapshot pairs. There is no new durable store,
generation counter, cursor or cross-request barrier. The one server-side state
is the short-lived Redis copy of the identities a reply implied, kept so the
relay can echo a digest instead of them ([request compaction](#request-compaction-w827-s2b)).

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

## Request compaction (W827 S2b)

On the live Data Bus message stream (2026-10-16, 6-hour window, Redis 8.10)
`worker.heartbeat` was 52% of the entries and about 76% of the bytes, 13.3 KB
per request at the median. Of its inner payload, `known_reply` was 4,825 bytes
at the median and in every request, `store_reads` 2,442 bytes in 31 of 50, and
`agent_sessions` 1,470 bytes in 40 of 50. The Data Bus keeps each request for
the message window (`DATA_BUS_MESSAGE_RETENTION_SECONDS`, six hours), so the
request size, not the reply, sets the stream's memory. Three compact forms
replace the full ones; **when** a heartbeat is sent is unchanged (W875).

| Full form | Compact form | Used when |
| --- | --- | --- |
| `known_reply` (every section's identities) | `known_reply_digest`: the SHA-256 (64 hex) of the canonical `known_reply` | the Board's previous reply for this scope offered exactly that digest as `known_digest` |
| `agent_sessions` (the session projection) | `agent_session_times`: per session only `session_id`, `heartbeat_at`, `last_inbox_check_at` | the same sessions, in the same order, changed in nothing but those two times since the projection the Board acknowledged |
| `store_reads` (every store's last read) | `store_reads_changed`: only the stores whose summary changed (all but `ms`/`at`, the existing comparison) | every store the Board acknowledged is still reported |

The observation groups change on every heartbeat (heartbeat, inbox, disk,
account times), so the identities the relay holds are new every time; a relay
cannot just say "unchanged". After building a reply the Board therefore keeps
`knowledge(snapshot, scope)`, exactly what the relay computes from the
snapshot it reconstructs, in Redis under its SHA-256
(`problem-board:heartbeat-known:v1:<bundle scope>:<digest>`, TTL 15 minutes,
longer than the 60 s active and 120 s idle relay ceilings). The reply marker
names it as `known_digest`. The relay echoes the digest only when it equals
the digest of its own knowledge; on the Board a missing, expired, corrupt or
foreign-scope entry is no echo at all, so a full reply, as before. A
mismatching digest cannot select a delta: the record is verified against the
digest and the scope before use. Redis being unavailable means nothing is kept
and no `known_digest` is offered. The cache is display data, never authority:
admission and the complete reply are built afresh on every call, as above.

The Board stores a times-only session report by updating those two columns of
the session's existing row; an older `heartbeat_at` never moves a session
back, as for a full report. `store_reads_changed` is merged into the stored
map. What it cannot apply (a session with no row, an unreadable report) it
does not apply, and it names the full form in the marker's `resend`; the
heartbeat succeeds.

**Mixed versions.** The marker's `features` lists the compact forms the Board
reads (`known_digest`, `agent_session_times`, `store_reads_changed`). A relay
uses a compact form only when the previous reply for that project listed it,
and only in place of the full form, never with it.

- *Old relay, new Board:* the relay sends the full forms; the Board answers as
  before and adds `features`/`known_digest` to the marker, which the old
  `decode_reply` does not read.
- *New relay, old Board:* the old marker has no `features`, so the relay keeps
  sending the full forms.
- *Rollback between two heartbeats:* a compact request reaches a Board that
  ignores the compact fields. The digest is then no echo, so a full reply.
  The reply names no features, so the relay drops its compact baselines and
  session/store-read signatures and the next heartbeat is full. Only in this
  case can a session change force that one extra heartbeat.

Every relay restart, reconnect, changed context or forgotten reply knowledge
also starts from the full forms.

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
