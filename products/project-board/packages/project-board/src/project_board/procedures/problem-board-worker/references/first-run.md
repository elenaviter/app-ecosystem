---
id: project-board.worker-reference.first-run
title: First Run
summary: Guide a user from an absent or unconfigured pb command to one attending worker by following the state and next action reported by the client.
tags: [procedure, project-board, worker, setup]
keywords: [pb status, install, setup, relay, enroll, authorize, attend]
see_also: []
---

# First Run: Guide The User To A Working Worker

Read this when the user asks you to use this skill and `pb status` does not
report `session_attending`, or when `pb` is not installed yet. The user may be
new to Problem Board. Your job is to make each step obvious: say what it is,
ask for what only the user knows, propose one command, run it after they
approve, and read the state again.

## Read The State First

```bash
pb status --runtime-kind <codex|claude-code> --runtime-session-id <this session id>
```

It is read-only, calls no server, and works before anything is configured.
It returns `state`, the machine's targets and relay, this session's enrollment
and projects, and `next`: the step, the command, and whether it needs the
user's approval (`approval: user`) or none. Act on `next` and run `pb status`
again after each step, because each step changes what the next one is.

## What The Setup Coordinates Mean

| value | what it names |
| --- | --- |
| target name (`--target-id`) | this machine's short, stable label for one KDCube deployment; the deployment never receives it |
| endpoint (`--endpoint`) | the real governed Problem Board MCP address on that deployment |
| tenant and platform project | the KDCube deployment coordinates that host the Problem Board app |
| host ID (`--host-id`) | the stable logical identity of this machine, published with its workers |
| host label (`--host-label`) | the machine's readable display name |
| allowed root (`--allow-root`) | a local directory workers on this machine may address |

The target name keys an isolated local tree under
`~/.kdcube/client-runtime/problem-board/targets/` and the default pointer used
by commands without `--config`. A second deployment gets a second target and
therefore separate relay configuration, profiles, mail field, logs, and client
source selector. It is not a remote identity and does not replace the host ID.
This section is the owning definition; setup guides point here rather than
restating the meanings.

## `pb` Is Not Installed

`pb` is the console command in the `project-board` distribution. A team host
installs its six-package client family from clean exports of approved App
Ecosystem and KDCube commits. Ask for both repository paths and full commits,
then propose the source install and selector from
[runtime actions](runtime-actions.md). The install is one dependency resolution
over all six first-party package paths:

```bash
APP_REPOSITORY=<app-ecosystem>
APP_COMMIT=<full-app-commit>
APP_EXPORT=$(mktemp -d)
KDCUBE_REPOSITORY=<kdcube>
KDCUBE_COMMIT=<full-kdcube-commit>
KDCUBE_EXPORT=$(mktemp -d)
test "$(git -C "$APP_REPOSITORY" rev-parse "$APP_COMMIT^{commit}")" = "$APP_COMMIT"
test "$(git -C "$KDCUBE_REPOSITORY" rev-parse "$KDCUBE_COMMIT^{commit}")" = "$KDCUBE_COMMIT"
git -C "$APP_REPOSITORY" archive "$APP_COMMIT" | tar -x -C "$APP_EXPORT"
git -C "$KDCUBE_REPOSITORY" archive "$KDCUBE_COMMIT" | tar -x -C "$KDCUBE_EXPORT"

python3 \
  "$APP_EXPORT/products/project-board/packages/project-board/scripts/install_from_source.py" \
  --source-root "$APP_EXPORT" \
  --kdcube-source-root "$KDCUBE_EXPORT"
"$HOME/.local/bin/pb" procedure install --target codex --target claude-code
```

Installing a command and a procedure changes the user's machine, so ask before
either command. Use only the targets they run. The source installer creates the
isolated client interpreter and guarded launcher, resolving all six
first-party distributions in one `pip install` invocation.
The repositories are inputs to the clean exports and never become runtime
import paths. After selection, `pb source status` reports the composite release
ID, both full commits, all six package trees, and the source loaded by the
relay.

## `machine_not_configured`

Explain in a few sentences: a Problem Board server is one KDCube deployment,
reached at an endpoint URL, holding its boards under a tenant and a project.
This machine needs to know which one, and it runs one small background relay
that carries messages between the agents here and that server.

When `next.step` is `configure_target`:

1. Ask which deployment they want. Ask for the endpoint URL, the tenant and the
   platform project, and say which is which, using the table above.
2. Say that the target name is only a local label they choose, and suggest one.
   The host id and host label name this machine, and they choose those too.
3. Ask which folders agents on this machine may work in. Those become
   `--allow-root` values.
4. Show the one `pb setup` command with their values and run it after they
   approve.
5. Select the same reviewed source that provided the bootstrap, now that the
   target configuration exists:

   ```bash
   pb source use-code \
     --repository <app-ecosystem> --ref <full-app-commit> \
     --expect <full-app-commit> \
     --kdcube-repository <kdcube> --kdcube-ref <full-kdcube-commit> \
     --expect-kdcube <full-kdcube-commit>
   pb source status
   ```

When `next.step` is `install_relay`: say that the relay runs in the background
for this user account and starts with the machine. Propose
`pb relay-service install` and run it after they approve.

## `relay_stopped`

The machine is set up and its relay is installed, but the relay is not
running. Someone may have stopped it on purpose, so say that it is stopped
and ask before proposing `pb relay-service start`. Do not reinstall it.

## `session_not_attending`

The machine is set up. List the configured targets from `pb status` by their
labels, and if there is more than one, ask which server this session should
work with.

- `enroll_session`: ask what this worker should be called on the board (the
  alias is only a display name), then run `pb worker listen` with this
  session's identity. It needs no approval.
- `authorize_profile`: the user signs in once in their browser to let this
  worker act for them. Give them the exact `pb worker authorize <profile>`
  from `next.command` to run in their own terminal, because the browser and
  their credential store are theirs.
  When that host has no browser, preserve the returned profile and append
  `--device`; the user opens the printed verification URL on another device,
  while the host CLI retains the private device code and writes the resulting
  credential to its native store. Do not combine device mode with callback
  flags.
  The same step appears for a worker that used to work and whose credential
  the relay can no longer use (its channel is `pending_authorization`). Then
  the command reconnects the worker to the Card it already has.
- `attend_project`: projects link workers on the board itself. Ask which
  project they want this worker in, and tell them to add this worker to that
  project in the board (the setup guide's section 8 walks it). Then run
  `pb status` again.
- `channel_disabled`: the operator turned this worker's channel off on this
  machine. Say so and ask them whether it should come back.

## `session_reconnecting`

The worker is enrolled and authorized, but the relay is reconnecting its
channel after a failure, so it cannot reach the board right now.
`session.connection` names the last error, the attempt count and the next
attempt time, and `next.step` is `wait_for_reconnect`. Say that in one
sentence and wait: the relay retries on its own. An accepted connection whose
handshake timed out is retried within seconds (up to a minute). A runtime
that is not there (`oauth_challenge_not_advertised`,
`oauth_mcp_endpoint_unreachable`, a refused connection) is retried every ten
seconds for fifteen minutes, then once a minute, and a relay restart retries
it at once. Other failures keep the normal backoff, up to thirty minutes.
Until then `pb coordinate`
refuses at once with `work_coordinate_channel_reconnecting`. Why: a worker
that cannot reach the board should learn that at once, not after a 90-second
deadline behind "the relay did not claim the operation".

## `session_attending`

This state means the channel works (`active`) and the worker attends a
project. Say which server (target label and endpoint), which project, and
which worker this session is, in one or two sentences, and stop. The rest of
this skill takes over from here.

## What Stays The User's

Setup, the relay install, browser consent and project membership are the
user's decisions. You guide and propose, and you run a command that needs
approval only after they give it. You never invent an endpoint, tenant,
project, profile or path, and you never ask for or handle a password or token.
