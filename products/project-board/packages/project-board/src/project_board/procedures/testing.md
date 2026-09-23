---
id: app-ecosystem.project-board.procedure.testing
title: Test The Problem Board Bundle
summary: Defines offline package, provider, shared-field, knowledge, widget, descriptor, and later live verification.
tags: [procedure, testing, bundle, problem-board]
keywords: [pytest, bundle suite, typecheck, MCP, browser proof]
see_also:
  - https://github.com/kdcube/applications/blob/main/playground/domain-solution/apps/problem-board@1-0/AGENTS.md
  - ./live-acceptance.md
  - https://github.com/kdcube/applications/blob/main/playground/domain-solution/docs/design/surface-map.md
  - https://github.com/kdcube/applications/blob/main/playground/domain-solution/docs/procedure-gaps.md
  - https://github.com/kdcube/applications/blob/main/playground/domain-solution/apps/problem-board@1-0/docs/topology-and-flows.md
  - https://github.com/kdcube/applications/blob/main/playground/domain-solution/apps/problem-board@1-0/docs/storage-and-retention.md
---

# Test The Problem Board Bundle

Before platform-backed tests, follow
[the platform suite procedure](repo:app-ecosystem/products/kdcube/procedures/platform-suite.md),
choose one interpreter, and prove imported KDCube and Connection Hub origins.
Provide this app path explicitly to the shared bundle suite.

Offline checks cover:

```text
problem-board pytest
Python compile/import and descriptor/OpenAPI parse
shared bundle contract suite with exact app fixture
complete named-service schema-tree build and capability-index preparation
problem_board widget tsc --noEmit and declared build
main scene tsc --noEmit and declared build
knowledge graph and SQLite rebuild
knowledge audience, surface, and stdio-MCP tests
```

## Run The Package Suite

The package suite imports the platform and the surrounding products, so the
interpreter needs seven source overlays on `PYTHONPATH`. Without them the run
fails with `ModuleNotFoundError` inside authorization and relay tests, which
reads exactly like a regression in unrelated code and is not one. Name the
overlays explicitly rather than trusting whatever the shell happens to have
installed, and keep them as checkout variables so this procedure stays free of
any one machine's paths:

```bash
KD=<kdcube-ai-app checkout>/app/ai-app/src/kdcube-ai-app
AE=<app-ecosystem checkout>
PB=<applications checkout>/playground/domain-solution/apps/problem-board@1-0
PROJECT_BOARD=$AE/products/project-board/packages/project-board/src

PYTHONPATH="\
$KD:\
$KD/kdcube_cli/src:\
$AE/packages/app-foundation/src:\
$AE/packages/service-foundation/src:\
$AE/products/connection-hub/packages/connection-hub/src:\
$AE/products/connection-hub/packages/connection-hub-cli/src:\
$PROJECT_BOARD" \
  <ai-app chat-processor python3.11> -m pytest "$PB/tests" -q -rs
```

The interpreter is the ai-app chat-processor environment, which carries the
platform's third-party dependencies and not necessarily what the overlays
declare. A bare system Python will fail on those before it reaches any Problem
Board code. Before the suite, run the dependency preflight with the same
interpreter over the six overlays' `pyproject.toml` files. It names the first
declared distribution the interpreter lacks in one sentence, and every line it
prints names the interpreter it asked, because the answer differs per host:

```bash
<ai-app chat-processor python3.11> "$AE/products/project-board/packages/project-board/src/project_board/procedures/dependency_preflight.py" \
  "$KD/pyproject.toml" "$KD/kdcube_cli/pyproject.toml" \
  "$AE/packages/app-foundation/pyproject.toml" \
  "$AE/packages/service-foundation/pyproject.toml" \
  "$AE/products/connection-hub/packages/connection-hub/pyproject.toml" \
  "$AE/products/connection-hub/packages/connection-hub-cli/pyproject.toml"
```

Why: on 2026-09-23 the dev-main chat-processor venv lacked `jwcrypto`
(connection-hub), `readchar` (kdcube-cli) and `json5` (connection-hub-cli), and
the suite passed until a change imported one at module level, when sixteen
modules failed to collect with errors that read nothing like the cause. The
preflight reads declared dependencies only: it cannot see an import that is
undeclared, a version present but outside its range, or extras. Exit 0 means
every declared distribution is visible to that interpreter, nothing more.

Why each overlay is there, and the third-party distributions it declares, so a
missing import is diagnosed rather than guessed:

```text
kdcube-ai-app             the platform SDK, conversation store, integrations
                          (declares nothing in pyproject; the venv is built
                          from the deployment requirements)
kdcube_cli/src            management errors raised by the authorization tests
                          (httpx2, rich, readchar, PyYAML)
app-foundation/src        the Data Bus the relay and worker stream ride on
                          (none required, extras mcp and native-secrets optional)
service-foundation/src    the service base the control plane extends
                          (none required)
connection-hub/src        card and grant models read by the worker stream tests
                          (aiohttp, cryptography, httpx, jwcrypto)
connection-hub-cli/src    profile authorization and card inspection
                          (filelock, httpx2, json5, keyring, mcp, platformdirs,
                          PyYAML, and the first-party app-foundation,
                          connection-hub, kdcube-cli, which the overlays supply)
project-board/src         the one shared contract imported by server and client
```

The platform checkout needs two entries, itself and `kdcube_cli/src`, because
the CLI keeps its own `src` layout inside that checkout and is not importable
from the platform root alone. Their order does not matter: both bind the same
top-level name and Python merges them as a namespace package.

The host-boundary subprocess rebuilds its own path from the six host
distribution roots in that overlay and explicitly refuses any
`kdcube_ai_app` import. It therefore proves that the launcher and its
transitive dependencies work without the platform while still testing the
standalone `kdcube-cli` distribution that Connection Hub requires.

Two additional checks run against a real installed host interpreter when the
caller names it explicitly. After the clean two-export install in
`add-a-worker-host.md`, run the same suite with:

```bash
PROBLEM_BOARD_HOST_PYTHON=<problem-board-venv>/bin/python \
PYTHONPATH="\
$KD:\
$KD/kdcube_cli/src:\
$AE/packages/app-foundation/src:\
$AE/packages/service-foundation/src:\
$AE/products/connection-hub/packages/connection-hub/src:\
$AE/products/connection-hub/packages/connection-hub-cli/src:\
$PROJECT_BOARD" \
  <ai-app chat-processor python3.11> -m pytest "$PB/tests" -q -rs
```

The ordinary overlay run skips those two installed-host checks with the exact
missing variable as its reason. The post-install run must execute them. A
failure naming a module rather than an assertion is a gap in the named source
or installed closure, not a product result.

The provider suite must build the complete schema tree, verify that every
cataloged action is available only on a compatible object-ref kind, and
prepare the capability index with the configured lexical/vector backend. An
app that loads while this preparation emits a warning has not passed provider
discovery acceptance.

The shared-field and control-plane suites must prove DAG validation, optimistic
revisions, worker-name binding, shard isolation, lease redelivery and one
settlement, destination receiver policy, cross-host mail materialization,
relay-versus-agent heartbeat evidence, assignment compare-and-set, stale-owner
rejection, stale-command supersession, suspended-worker ignored attempts and
non-replay, confirmed worker retirement and retained attribution,
source-scope collision, stateless packet hydration, safe projection filtering,
exact replay, changed-content idempotency conflicts, and relay idempotency.
Project-report checks must prove both halves of one contract
(`project_board.contract.project_report_contract`). On the reporting machine:
a submission the service would refuse is refused before anything is queued,
the exact unexpired model-owned mail lease is required, attachment bytes are
snapshotted before returning, only the same summary and file hashes replay, and
the command offers the settle command for a stored report and for nothing else. On the
service: counts, the delta and blocked work are composed from its own rows, the
delta is capped and ordered newest first with a reason on every row, cancelled
work stays distinct, a dry run stores nothing, a retry is compared by the
author's fields only, and a whole pre-contract document still delivers.
`project.report.publish` and `project.report.fail` are dispatched through the
relay. The next report's window starts at the predecessor's publication time.
No report path may read a plan: a change that makes one do so fails review
whatever its tests say. A change to the delta statement is also run against a
real PostgreSQL in a scratch schema, because the in-memory store mirrors its
semantics and cannot prove its SQL.
It must prove that watch exposes availability without bodies or leases, receive
creates the model-owned leases, and only one current worker-listener record is
reported to the control plane. Sender discard checks must cover pending remote
suppression, pending LOCAL suppression, an explicit soft notice after receipt,
sender ownership, multiple targets, idempotency, and a suspended recipient.
It must also prove attachment-only operator requests and replies, no implicit
project attendance from the conversation composer, and an explicit assignment
boundary for work refs. Direct owner-worker requests and replies must work for
an owned worker with zero attendances. Worker-to-worker mail must still require
a shared project and receiving-machine policy.

Attachment delivery checks must start with the signed operator control, fetch
the exact bytes into the local field, and prove the same first-class manifest
through `worker receive` and `worker lease-read` for Codex and Claude Code.
They must run the manifest's session-bound `worker attachment-read`, reject a
different lease owner or changed bytes, settle the message, and prove that the
complete processed record and attachment bytes remain readable. Include both
an attachment-only request and an attachment-only reply.

Attendance checks must prove that a worker can have zero or one current
project. A same-project link is idempotent. A link to a different project is
refused with `work_worker_already_attends_project`, the current and requested
project refs, and the instruction to unlink first. The store must serialize
concurrent link attempts on the stable worker row and must not recreate
attendance after concurrent retirement. The project board and first-worker
chooser show no Link action for a worker attending elsewhere and name that
current project instead.

Timeline checks must prove server-side text, date, status, and worker filters;
bounded paging; one row per incoming owner-mail artifact; stable artifact refs;
and exact mailbox links only for owner-worker turns. Thread-window checks must
select the latest bounded controls and inbox rows and return that window oldest
to newest, so an old full page cannot hide current mail in a long-running
worker conversation.

The provider invocation suite must prove that `object.get(work.project)`
returns every collection advertised by its schema: linked workers, current
assignments, controls, and `latest_events`. This is a response-contract check,
not only a static schema assertion.

The worker transport suite is separate from the model-facing provider suite. It
must prove:

```text
relay presents its existing Card bearer directly; no MCP bootstrap or token exchange
admission binds tenant + platform project + bundle + exact service resource
transport user/card principal does not replace the grantor who owns the worker
Data Bus payload.operation, MCP tool name, and Named Services action use the same canonical operation ID
all three surfaces enter one service-owned handler table
MCP and Data Bus call the same live-Card guard before that handler
observation, coordination, and relay operations require their exact declared grant
journal view publication additionally requires work:journal:view
another card's partition is rejected
ingress acknowledgement is not treated as handler completion
outcome-unknown retry preserves message_id and idempotency_key
Card revocation rejects incoming publication and removes outbound live routing
authority lookup failure fails closed without pretending revocation
committed control precedes reference-only push
push wakes the host-relay runtime before its reconciliation interval
disconnect, token expiry, or Card revocation removes server-decided stream presence
pending authorization promotes only after stream connect and worker publication
a local profile-store change wakes pending authorization before the poll interval
retryable authorization/metadata failure opens one durable local interval with its exact code and first observed instant
host inspect, relay-service status, and worker list expose that local interval while the governed route is unavailable
the first successful authorized cycle closes and publishes the interval without making recovery depend on publication
an unacknowledged interval remains locally pending and is offered again
the host-local live-test switch requires explicit interruption confirmation, accepts only an active exact worker and an allowlisted retryable code, wakes the existing relay, and is consumed once
an absent, consumed, expired, malformed, or manually cleared switch cannot interrupt a worker channel
the selected session observes control_plane.connected exactly once
repeat worker listen does not regress an already published local state
session-resume publication comes from the addressed host's approved roots
session-resume identity/hash mismatch and receiver-policy denial are rejected
only the requesting owner can read or close the expiring resume view
close and expiry erase the command body and hash
```

The journal suite must prove strict portable-ref parsing, symlink traversal
rejection, one user/project REMOTE binding with optimistic revision, lifecycle
creation of `problem_board_project_journal_bindings`, agent-authored free-form
front-matter Markdown in the mapped Git home, strict explicit indexing for new
entries, searchable legacy entries with visible compatibility diagnostics,
receipt-only field records, full-front-matter and body SQLite FTS retrieval,
relay reconciliation, and reconstruction in a fresh LOCAL workspace from the
same repository. It also resolves a portable project-artifact
ref to the correct LOCAL source directory while keeping that absolute path out
of the REMOTE contract.

Project-isolation checks must create two projects, link the worker to only one,
and prove that heartbeat and `control.pull` for the other return
`work_worker_not_linked`. After `project.unlink_worker`, the worker remains in
the pool but can no longer pull, publish, or receive new controls for that
project. A later link must reuse the same worker identity, native-session and
credential binding, while prior conversations, project events, assignment and
control history, mail, journals, and settlement records remain attributable.
The new attendance has its own creation time. Unsettled controls withdrawn by
unlink remain withdrawn rather than reappearing as direct mail.

A coordinated live window follows `live-acceptance.md` and additionally proves
app readiness, the generic Named Services route for a supervisor, the direct
Card bearer admission for each worker, canonical action parity across Named
Services, MCP, and Data Bus, exact grants, missing-grant denial, live revocation, addressed
push, browser desktop/mobile behavior, event/projection refresh, a user-started
Codex session receiving a standard inbox-check instruction, a Claude Code
session owning one notification-only watch, short burst coalescing, silent idle
checks, failure backoff, model-owned receive and settlement, watch teardown
before detach, centralized multi-message discard before and after receipt, stop
receipt, and restart reconstruction. Report each proof as pending until it is
actually run.
For journal continuation, the live run also proves that machine B uses a fresh
workspace/index and that REMOTE rows contain no absolute path or journal body.
Browser acceptance also verifies the compact action menu, direct conversation
default, optional attended-project context, attachment-only send, compact
project worker rows, timeline filtering and mailbox jumps, labeled and copyable
native session ID, the terminal action's host-generated command, the permanent
Board plus Connection Hub scene, and the worker shield action opening the exact
card by `access_id` on desktop and mobile. It also proves the scene does not
mount a coordinator chat frame.
