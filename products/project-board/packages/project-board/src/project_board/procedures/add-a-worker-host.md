---
id: app-ecosystem.project-board.procedure.add-a-worker-host
title: Add A Worker Host
summary: Step by step, for a person or for an agent acting for them, to turn a remote Linux machine on a private network into a host that runs Project Board agent sessions with access to exactly the repositories they work on.
tags: [procedure, problem-board, setup, relay, worker, linux, headless]
keywords: [worker host, tailnet, headless, deploy key, pinned client, systemd relay, keyring, device login, bypass permissions, private code]
see_also:
  - ./first-time-setup.md
  - ./operator.md
  - repo:app-ecosystem/products/project-board/docs/add-a-machine.md
  - repo:app-ecosystem/docs/connection-hub/package/delegated-cards.md#descriptor-owned-authorization-profiles
---

# Add A Worker Host

This procedure adds a machine to an existing Project Board deployment: a remote
Linux machine, usually without a screen, reached over a private network such as
Tailscale. Agent sessions (Claude Code or Codex) run there, each in its own
workspace with its own clones of the repositories it works on, and talk to the
board through that machine's relay.

The first machine of a deployment is set up with
[first-time setup](first-time-setup.md). This document covers a remote,
headless one. It was rehearsed end to end on a first such host on 2026-09-22,
and every problem met there is either fixed or written below as a **Known gap**
with its workaround and the work item that removes it.

The short version a user follows, and what they hand their setup agent, is
[add a machine](repo:app-ecosystem/products/project-board/docs/add-a-machine.md).

## Who does what

- **Operator**: the person who owns the Project Board project.
- **Host agent**: an agent with an SSH session on the new host, acting for the
  operator. A person can do every host-agent step too.
- **Either**: the host agent, or a person instead of it.
- **Machine administrator**: whoever holds `sudo` on the machine. Only system
  packages and root-owned files need them.

Every step, in the order it happens:

| step | who | what | why that person |
|---|---|---|---|
| 0 | **operator** decides, host agent proposes | one proposal: host id and label, Linux user, **the project the agents join**, one workspace per agent, agent names, runtimes and accounts, who approves the Cards, and whose mail may reach the agents | these are the operator's choices; nothing changes before approval |
| 1 | **operator** | an SSH login to the machine for the host agent | only the operator holds that access |
| 1 | either | check Python, git, other users, home folder permissions, `tmux` | routine |
| 1 | **machine administrator** | install `tmux` when it is missing | a system package needs `sudo` |
| 2 to 3 | either | install and configure `pb` from the approved commits and the worker procedure | routine |
| 4 | **machine administrator** | let the user's services survive logout (`sudo loginctl enable-linger`) | a system setting needs `sudo` |
| 5 | either | install the coding agent runtimes in the user's home | routine |
| 5 | **operator** (or the account owner) | log each runtime in with the account it should use | it is their account |
| 6 | **operator** | type the credential store password once per boot (until W258) | the password is theirs to keep |
| 6 | either | install the relay service | routine |
| after 6 | **machine administrator** | on a host set up before the user installer, remove the root-owned `/opt` install | removing root-owned files needs `sudo` |
| 7 | host agent | print a revoke sheet for any deploy key made on this host before W942; it makes none | routine |
| 7 | **operator** | delete those deploy keys on GitHub, when they decide | only a repository admin can revoke access |
| 7 | **the person** (each agent's owner) | link GitHub on My Card (**Set up GitHub** on their card on the board), set their commit email there, and install the GitHub App on each repository owner | the key is theirs; agents act as them |
| 7 | host agent | run `pb worker connect-project` and read `github_key: ready` per repository | routine |
| 8 | either | one empty workspace per agent | routine |
| 9 | host agent | write one start script per agent, then start one `tmux` session per agent with it | routine |
| 9 | **operator** decides, host agent applies it | approve unattended command mode: Claude Code's one-time bypass warning or Codex's `--ask-for-approval never` policy | it is the operator's risk decision; the host user and repository deploy keys remain the boundary |
| 9, existing agents | **coordinator** announces the window, host agent moves one agent at a time | move an agent set up before 2026-09-25 to `~/.kdcube/pb/workspaces/<alias>` with its conversation, memory and worktrees, or roll it back | the operator ruled it (2026-09-26); it stops each agent briefly |
| 10 | the agent, inside its session | enroll and report the profile to authorize | routine |
| 11 | **operator** | approve each agent's Card, with a code, in a browser on any device | the agent acts in the operator's name |
| 11 | host agent | tell each approved agent to start listening | routine |
| 12 | **operator** | add the agents to the project | who works on the project is the operator's decision |
| 12 | the agent | set up its workspace from the project record: clone each repository on the card | the project card, not the host, names the repositories |
| 12 | **operator** approves each check, the coordinator runs it | the seven checks, then a first small item | the operator decides when an agent is part of the team |
| 13 | either | move the selected client source, restart the relay and the sessions | a coordinated runtime action |
| 14 | either, with the **operator** for GitHub | retire an agent or the host, delete its deploy keys | routine; only an admin deletes a key |

The host agent does everything else over its own SSH session. The operator's
own terminal on the host is needed only for the password in step 6.

The machine's administrator installs `tmux` (step 0) and, on a host set up
before release environments, removes an old root-owned `/opt` install after
the migration proof in step 13. Both need `sudo`.

**Every operator step states, in the step, why a person is required and what it
commits them to afterwards.** An operator step exists because of an identity or
a secret, never because a command is awkward. Step 6 leaves the operator holding
a password, so it says so before they type one.

## What you end up with

```text
new host
└── /home/<user>/                      the Linux user that runs the agents
    ├── .local/bin/pb                  inert current-release launcher
    ├── .kdcube/client-runtime/tools/problem-board/
    │   └── releases/
    │       ├── current -> <release-id>                  atomic host selection
    │       └── <release-id>/venv/                       complete release environment
    ├── .kdcube/          (700)        selectors, snapshots, relay state, logs, mailboxes
    ├── src/app-ecosystem              the public clone the client is built from, read only
    ├── .config/systemd/user/kdcube-problem-board-relay-*.service
    └── workspaces/       (700)
        ├── <workspace-1>/             one agent, its own clones
        └── <workspace-2>/             another agent, its own clones
```

**The client release is selected once per host.** `releases/current` selects
one complete environment used by `~/.local/bin/pb` and every relay definition
on the host. Each target retains its own source-action receipt, so status can
show which approved released version or App Ecosystem commit was
applied for that target. Agent workspaces are build workspaces rather than
runtime import paths. The host release moves only by
[step 13](#13-update-the-selected-client-source).

**What the install brings, and what it does not.** Steps 2 to 6 install, for
one Linux user: the inert `pb` launcher, the current client release environment
(`project-board` with its Connection Hub client and foundation packages), the
worker procedure package for Claude Code and Codex, and the user relay service.
They do not install Project Board itself, which is an app on the KDCube
deployment the relay talks to, nor any KDCube or Connection Hub server. The host
needs neither: it reaches the deployment through its endpoint.

**Private state stays private.** A shared machine has other users, and a home
directory is often readable by a shared group. The selected code snapshots,
workspaces, and `pb` state are readable only by the user who runs those agents.
Every step that writes code checks this.

## 0. Decide before starting

Runs on: the operator's machine, as a conversation with the host agent. Nothing changes on the host yet.

The host agent presents these as one proposal, and the operator approves it
before anything changes:

| decision | example | why it matters |
|---|---|---|
| host id and label | `host-two`, "host-two (Linux, headless)" | the machine's name on the board. "The agents on a host agree before a relay restart" is per host id. |
| Linux user that runs the agents | `agent-user` | owns the keys, the relay and the credential store |
| **the project the agents join** | `Project one` | its project card lists the repositories (alias, URL, role). Step 12 clones them into each agent's workspace with the owner's GitHub key (step 7). The agents reach nothing else through Project Board. Which repositories a project uses is decided on its card, not per host (W304 finding 15). |
| workspaces, one per agent | `~/.kdcube/pb/workspaces/agent-one`, `~/.kdcube/pb/workspaces/agent-two` | each agent edits only its own clones. The root is `~/.kdcube/pb/workspaces/<alias>` on every host (operator ruling, 2026-09-25: not in the user's home folder). An existing host keeps its old folders until a planned move. |
| agent names | `agent-one@host-two`, `agent-two@host-two` | display names on the board. The board addresses a worker by a stable generated name. |
| runtime of each agent | `claude-code` for both, or one of each | an agent session is described by its runtime, provider account, and session ID. Its stable worker address remains runtime plus session ID, and the board keeps its first host-login reading, as inference. One host can run Claude Code and Codex agents side by side, each in its own workspace. Needing another runtime means adding another agent. |
| account per runtime | the Claude account for Claude Code agents, the OpenAI account for Codex agents | step 5 logs each runtime in once. Every agent of that runtime under the same Linux user shares its login and its usage. |
| who approves the agents' Cards | the project's operator | the KDCube user the agents act for. Until a project can have more than one operator (W260), it is the project's operator. |
| `tmux` on the host | installed by whoever administers the machine | step 9 runs each agent in it. It is a system package, so a user-level install cannot provide it. |
| **teammates who may write to these agents** | `*` (the default), any agent that shares a project with them, or named workers such as the coordinator | the host's receiver policy (`receiver_policy.allowed_peer_workers`). `pb setup` writes `*` (operator ruling, 2026-09-26); step 3 narrows it only when the operator chose named workers or none. The board lets an agent in a shared project address them, and, while one of them attends no project, anyone who knows its exact stable name (never its alias) may send it a request, reply or ping; so `*` means "my project teammates, and whoever knows a new agent's id before it joins". It grants no access to anything: it decides whose mail reaches these agents. The operator's own messages reach them either way. |
| **how the client is installed** | a published `project-board` version (for example `2026.9.26.1900`), or an exact App Ecosystem commit | step 2. A published release is one version from the package index, the default for a machine that only runs agents; exact commits are for a machine of the team that builds Project Board, or before a release is published (W304 U2). |
| **GitHub access** | each agent's owner links GitHub on their My Card for the project, sets their commit email there, and the deployment's GitHub App is installed on each repository owner ([GitHub access for agents](repo:app-ecosystem/products/project-board/docs/github.md)) | agents push, open pull requests and post review verdicts as their owner, over HTTPS with `pb worker git-credential` and `pb worker gh`; nothing is signed in on the machine. There is no other route: no machine account signed in to `gh`, and no deploy key (W942). |

**Repositories are not a host decision.** They belong to the project: the
operator sets them on the project card (Team, project card, repositories), and
every host whose agents attend the project follows that list. To read it, the
host agent asks the operator, or reads it from the board as any agent already
attending the project does:

```bash
pb worker context --project-ref <project> --format brief | grep '^repositories\['
```

Adding or removing a repository for the project is a change to the card; the
agents run `pb worker connect-project` again (step 12).

## 1. Give the host agent access to the host

Runs on: the operator's machine (the login), then the host (the checks).

**Operator.** Give the host agent an SSH login to the machine as the user that
will run the agents: a key it can use and the user name.

```bash
ssh -i ~/.ssh/<key> <user>@<host>      # what the host agent runs
```

That session must be a **direct** login as that user. A shell reached with `su`
from another account has no user-session environment (`XDG_RUNTIME_DIR`, the
user bus address). The relay service and the credential store both need it,
and so does everything started from that shell, including the agent sessions.

**Known gap (W258):** in a `su` shell, `pb relay-service start` fails with
`Failed to connect to bus: No medium found`, and `pb relay-service status`
reports a running relay as stopped. Workaround in such a shell:
`export XDG_RUNTIME_DIR=/run/user/$(id -u)`.

**Host agent.** Check the machine:

```bash
whoami; uname -srm; head -2 /etc/os-release
python3 --version; git --version
echo "$XDG_RUNTIME_DIR" "$DBUS_SESSION_BUS_ADDRESS"   # both set
sudo -n true && echo sudo-ok
command -v tmux || echo "tmux missing: ask the machine's admin to install it (apt install tmux, or dnf install tmux)"
command -v gh || echo "gh missing: brew install gh (macOS); apt install gh, or the GitHub CLI apt repository (Debian, Ubuntu); dnf install gh (Fedora)"
getent passwd | awk -F: '$3>=1000 && $3<60000 {print $1}'   # other users on the machine
stat -c '%A %G %n' ~                                         # who can read the home directory
```

Needed: Python 3.11 or newer, git, the GitHub CLI (`gh`), a user-session
environment, sudo. `gh` must be installed but needs no sign-in: with the
owner's GitHub key, `pb worker gh` hands it the token for each command, and the
relay finds it by its absolute path even under a service's short `PATH`. Note the
other users and the home directory's group: they decide how much the user home,
client state, and workspaces must lock down.

## 2. Install The Pinned `pb` Bootstrap For The Agent User

Runs on: the host.

Two routes install the same client. Step 0 decides which (W304 U2):

- **A published release** (the default for a machine that only runs agents):
  the operator names an approved `project-board` version on the package
  index, and the host installs that version with the packages it depends on
  (`app-foundation`, `service-foundation` and `connection-hub[client]`, all
  from the index; no KDCube package and no Connection Hub command line, W322).
  This route works from the first release that publishes those four packages
  together; before it, use exact source commits:

  ```bash
  python3 -m venv "$HOME/.local/share/project-board-bootstrap"
  "$HOME/.local/share/project-board-bootstrap/bin/python" -m pip install "project-board==<approved-version>"
  ```

  Step 3 then runs `pb setup` through
  `"$HOME/.local/share/project-board-bootstrap/bin/pb"` and selects the
  release with `pb source use-release --expect-version <approved-version>`
  instead of `pb source use-code`. What each route means and what
  `pb source status` reports after it is in the skill's
  [first run](problem-board-worker/references/first-run.md) reference.
  Moving the machine to another version later, or back to one it ran, is
  [install, update, roll back pb](install-update-rollback.md).
- **Exact source commits** (a machine of the team that builds Project Board,
  or before a release is published), below.

**Host agent**, logged in as the user who will run the agents, first clones the
source repository the client is built from. It is public, and this clone is the
install source only, read and never edited: each agent gets its own clones in
step 8.

```bash
mkdir -p ~/src
[ -d ~/src/app-ecosystem ] || git clone -q https://github.com/elenaviter/app-ecosystem.git ~/src/app-ecosystem
git -C ~/src/app-ecosystem fetch -q origin
```

Then it installs the client family from a clean export of the exact App
Ecosystem commit approved by the operator:

```bash
APP_REPOSITORY=/home/<user>/src/app-ecosystem
APP_COMMIT=<approved-full-commit>
APP_EXPORT=$(mktemp -d)
test "$(git -C "$APP_REPOSITORY" rev-parse "$APP_COMMIT^{commit}")" = "$APP_COMMIT"
git -C "$APP_REPOSITORY" archive "$APP_COMMIT" | tar -x -C "$APP_EXPORT"
python3 \
  "$APP_EXPORT/products/project-board/packages/project-board/scripts/install_from_source.py" \
  --source-root "$APP_EXPORT"
```

The source installer calls the same release builder as later source switches.
It creates `releases/<release-id>/venv`, resolves and smokes the complete
candidate, atomically moves `releases/current`, and installs launcher version
2. Its one resolver invocation binds `project-board`, `app-foundation`,
`service-foundation`, and `connection-hub` (with its `client` extra) to the App
Ecosystem export; package indexes provide only third-party dependencies. The
client needs no KDCube source and no Connection Hub command line (W322). Do not use
`sudo`, a checkout launcher, or an editable install. Repeat the install for
another login user rather than sharing one credential-bearing runtime between
users.
After this first installation, `pb source use-code` owns code-source updates.
The bootstrap installer recognizes an installed stable-current relay definition
and directs the operator to that host transaction; the exact lifecycle is in
step 13 and the installed worker procedure's `references/runtime-actions.md`.

A host set up before release environments may still hold either a root-owned
environment under `/opt` or the user-owned
`~/.kdcube/client-runtime/tools/problem-board-venv`. Install the current source
as above. The relay service continues its existing process until step 6 writes
the stable `releases/current/venv/bin/python` definition and verifies its
startup record. The exact proof required before either old environment is
removed is in [step 13](#migrate-an-existing-host).

## 3. Configure `pb` for the user that runs the agents

Runs on: the host.

**Host agent**, as that user:

```bash
pb setup \
  --target-id <target> \
  --endpoint <problem_board MCP endpoint of the deployment> \
  --tenant <tenant> --platform-project <project> \
  --host-id <host-id> --host-label "<label>" \
  --allow-root /home/<user>/.kdcube/pb/workspaces
pb host show     # receiver_policy.allowed_peer_workers: ["*"] unless step 0 chose otherwise
chmod 700 ~/.kdcube
# A published release (step 2's first route) replaces the next command with:
#   pb source use-release --expect-version <approved-version>
pb source use-code \
  --repository /home/<user>/src/app-ecosystem \
  --ref <approved-full-commit> \
  --expect <approved-full-commit>
pb source status
pb procedure install --target claude-code --target codex
pb procedure verify
pb relay --once          # zero workers, success
```

`pb setup` gives a new host the default teammate list `*`: every agent that
shares a project with these agents may write to them. Only when step 0 chose
otherwise, set the whole list: `pb host configure --allow-peer-worker <name>`
(repeat the flag for several names) or `pb host configure --deny-all-peers`.
A host set up with a client from before 2026-09-26 may still hold an empty
list and refuse every teammate's mail (as the first host did until 2026-09-24): run
`pb host configure --allow-peer-worker '*'` there. A host file that has **no**
`allowed_peer_workers` key at all changes from "nobody" to `*` when the client
is upgraded, with no command; an explicit empty list (`[]`, what
`--deny-all-peers` writes) stays "nobody". Run it again later to change the list, then restart the
relay (a coordinated runtime action) so it reads the new policy.

The procedure is installed after `pb source use-code`, because the selected
client carries the revision. `pb procedure verify` proves the installed
procedure matches it. A stale or unverifiable package is a defect in this
path, recorded and fixed before the walk goes on, never skipped: on a host that
joined the team earlier the package was eight revisions behind.

The endpoint is the one the first machine uses (its `relay.json`,
`target.endpoint`). It is the deployment's public address and is never written
into shared documents.

**If a project coordinator will run on this machine** (optional): the operator
runs, once, `pb host configure --add-control-kind project.file.edit`. A person editing a
project file on the card has the edit applied by the coordinator's relay, and a
machine accepts such edits only after this opt-in; until then the card refuses
the edit and names this command. It is per machine (the relay's receiver
policy), and one run covers every project coordinated here. The edit's pull
request opens under the coordinator owner's GitHub key, so `gh` must be
installed on this machine (step 1); it needs no sign-in. Use
`--add-control-kind`, never `--allow-control-kind`, which replaces the whole
list and would refuse mail, requests and pings. Then restart the relay (a
coordinated runtime action) so it reads the new policy.

**Known gap (W261):** `pb` creates `~/.kdcube` readable by the home directory's
group, which let another user list the mailboxes and relay configuration on the
first machine. `chmod 700` closes it until W261 lands.

### A second KDCube on this host: KDCube profiles

One host can serve two KDCube deployments at once, for example a local KDCube
and a cloud one, each with its own relay (operator, 2026-10-18: "Two relays at
once: << but this we nee3d"). Each deployment is a **KDCube profile**: the name
`pb setup --target-id` gives it. A KDCube profile is not the Card authorization
profile of step 11 (`pb worker authorize <profile> --device`), which names one
agent's credential. One agent session works for one KDCube profile.

1. Set up the second profile with its own `--target-id` and endpoint, as
   above. Setup keeps the existing machine default, so agents that run `pb`
   without a profile stay on their KDCube; pass `--make-default` only to move
   them. The result names the profile and whether it is the default.
2. Install its relay as a second service, an operator-approved step like
   step 6: `pb relay-service install --profile <name>`. Each relay service is
   pinned to its own configuration, so the two run side by side.
3. Start each agent session for that KDCube with `PB_PROFILE=<name>` in its
   environment (add `export PB_PROFILE=<name>` to that agent's step 9 start
   script), then enroll, authorize and
   attend as in steps 10 to 12. The skill's commands pass no option, so the
   environment is what keeps every command of that session on its KDCube.

Every host command also takes `--profile <name>`. A command picks its KDCube in
this order: `--config <path>`, `--profile <name>`, `PROBLEM_BOARD_CONFIG=<path>`,
`PB_PROFILE=<name>`, then the machine default. An unknown name is refused
(`work_profile_unknown`, with the known names), and so is a name set up for two
deployment projects (`work_profile_ambiguous`); neither falls back to the
default. `pb status` and `pb worker context` show the profile in use and what
chose it.

An agent's resume line names its KDCube profile (`env PB_PROFILE=<name> ...`),
and the board shows that word only to a platform super-admin. Everyone else
sees the regular single-profile line (operator, 2026-10-18: "if the user is
admin we can show him the instruction with the specification of the profile.
otherwise regular single profle instruction"), which resumes a session of a
non-default profile on the machine's default board. On such a machine, resume
it with `PB_PROFILE=<name>` set. Installing a pb version (`pb source
use-release`) restarts every relay on the machine, the default board's
included.

## 4. Keep the user's services running

Runs on: the host.

**Host agent:**

```bash
sudo loginctl enable-linger <user>
loginctl show-user <user> -p Linger      # Linger=yes
```

Without it, the relay stops when the last SSH session of that user closes.

## 5. Install the coding agent and log it in

Runs on: the host. The login's browser approval can be on any machine.

**Host agent**, for Claude Code, without touching the system: Node from its
official build into the user's home, then the CLI.

```bash
V=$(curl -fsSL https://nodejs.org/dist/index.json | python3 -c 'import json,sys;print(next(r["version"] for r in json.load(sys.stdin) if r["lts"]))')
A=$(uname -m | sed 's/aarch64/arm64/;s/x86_64/x64/')
mkdir -p ~/.local/node
curl -fsSL https://nodejs.org/dist/$V/node-$V-linux-$A.tar.xz | tar -xJ -C ~/.local/node --strip-components=1
export PATH=$HOME/.local/node/bin:$PATH
npm install -g @anthropic-ai/claude-code
grep -q '.local/node/bin' ~/.bashrc || echo 'export PATH=$HOME/.local/node/bin:$HOME/.local/bin:$PATH' >> ~/.bashrc
claude --version
```

**Operator** (or the person whose account the agents use): log in once, in an
SSH session as that user. Run `claude`, choose the account, open the printed URL
in a browser on any machine, approve, and paste the code back. The login lands
in `~/.claude/.credentials.json` and every later session of that Linux user
uses it, including `--resume` and fresh sessions. It changes only on `/logout`.

**Host agent**, for Codex, when any agent in step 0 uses it: the same Node, then
the CLI.

```bash
export PATH=$HOME/.local/node/bin:$PATH
npm install -g @openai/codex
codex --version
```

The relay resolves the documented user install directly at
`~/.local/node/bin/codex` and adds that directory to the queued command's
`PATH`. Its service environment therefore does not need to inherit an
interactive shell's `PATH`. Verify the executable before leaving step 5:

```bash
test -x ~/.local/node/bin/codex && ~/.local/node/bin/codex --version
```

**Operator** (or the person whose account the Codex agents use): log in once, in
an SSH session as that user. On a headless host, run
`codex login --device-auth`, open the address it prints on any device, enter the
code, then run `codex login status`. Device login is the normal path when it is
enabled for the ChatGPT workspace.

When device login is unavailable, use Codex's localhost callback through SSH.
On the operator's machine, run
`ssh -L 1455:localhost:1455 -i <key> <user>@<host>` and keep that connection
open. In its remote shell, run `codex login`, open the printed URL in the
operator's local browser, and finish with `codex login status`. The browser's
callback to local port 1455 reaches Codex through the tunnel. The login lands in
`~/.codex/auth.json`, and every Codex session of that Linux user uses it.

**Why a person:** each login is an account and its usage. Every agent of that
runtime under this Linux user works under it, which is why step 0 names the
account per runtime.

The host also keeps the public account description used to identify the
worker: Claude Code publishes it as `oauthAccount` in `~/.claude.json`; Codex
publishes an account ID and public identity claims in `~/.codex/auth.json`.
`pb worker authorize` reads only the account ID, email, and organization from
that local runtime state. Credential and token values stay in their native
files and never enter Project Board, Connection Hub client metadata, command
arguments, or logs. A runtime may use an API key or otherwise omit this public
account description. In that case authorization continues without the
identification metadata and reports `Provider account not reported`.

## 6. Give the relay a credential store, then install it

Runs on: the host.

**What it is.** The relay keeps each agent's credential in the user's password
store (Secret Service; on Linux, gnome-keyring: the encrypted file
`~/.local/share/keyrings/login.keyring`, protected by a password). A machine
reached only over SSH has nobody to unlock it at login, so the **operator**
unlocks it, in an SSH session of their own as that user, because the password
is theirs: once when the host is set up, and **again after every reboot**.
Until it is unlocked, no agent on the host can authorize or reach the board.

**Unlock** (also the first time: then it creates the store with the password typed):

```bash
printf 'Password store password (not shown): '; read -rs P; echo; if [ -n "$P" ]; then systemctl --user stop gnome-keyring-daemon.socket gnome-keyring-daemon.service 2>/dev/null; pkill -u "$USER" -x gnome-keyring-d; printf %s "$P" | gnome-keyring-daemon --replace --unlock --components=secrets >/dev/null; echo "done; now run the check"; else echo "the password is empty; nothing changed"; fi; unset P
```

- **Nothing is shown while typing.** Type the password and press Enter.
- **Which password:** the one the store was created with, the first time this line ran on this host. Keep it in a password manager; nothing on the machine can remind you of it.
- **Why it stops the service first** (W558): on a host where a systemd user unit (`gnome-keyring-daemon.service`, socket-activated) owns the Secret Service, the unit's daemon keeps the name `org.freedesktop.secrets` locked, and `--replace` alone starts a second daemon the board never talks to. Stopping the unit, its socket and any leftover daemon first lets the new, unlocked daemon take the name. Proven on host `mint` (Linux Mint 22.3, gnome-keyring 46.1) on 2026-10-05. On a host without the unit the stop finds nothing to stop.

**Check** (each line's expected result after `#`):

```bash
busctl --user status org.freedesktop.secrets | grep '^PID='   # the same number as the next line
pgrep -u "$USER" gnome-keyring-d
timeout 15 "$HOME/.kdcube/client-runtime/tools/problem-board/releases/current/venv/bin/python" -c 'import keyring as k; k.set_password("pb-probe","p","x"); print(k.get_password("pb-probe","p")=="x"); k.delete_password("pb-probe","p")'; echo "exit $?"   # True, exit 0
```

Two different PIDs, or no `True` within 15 s (`exit 124`), means the password did not open the existing store: run the unlock again with the right password, or reset.

**Only these lines.** The host agent gives the operator exactly the unlock, check and reset above, never another keyring command of its own: on mint (2026-10-05) an improvised `gnome-keyring-daemon --unlock` left a stray daemon that broke the store further, and a restart of the keyring unit without asking added another (W558). If the check fails twice, stop, change nothing more, and report what each line printed to the coordinator.

**Reset** (the password is lost, or the first one was mistyped). It asks the new password twice and changes nothing unless both entries match; then it sets the old store aside and creates a new, empty one:

```bash
printf 'New password: '; read -rs P; echo; printf 'Again: '; read -rs Q; echo; if [ -n "$P" ] && [ "$P" = "$Q" ]; then systemctl --user stop gnome-keyring-daemon.socket gnome-keyring-daemon.service 2>/dev/null; pkill -u "$USER" -x gnome-keyring-d; mv ~/.local/share/keyrings/login.keyring ~/.local/share/keyrings/login.keyring.old-$(date +%Y%m%d-%H%M) 2>/dev/null; printf %s "$P" | gnome-keyring-daemon --replace --unlock --components=secrets >/dev/null; echo "store created"; else echo "the two entries are empty or differ; nothing changed"; fi; unset P Q
```

Then run the check. A reset loses only what the old store held: each agent on this host authorizes again (step 11).

**A relay that was already running** (a reboot, or a store unlocked after the relay started) picks the unlock up on its next attempt, within about a minute, with no restart: while the store is locked it refuses by name (`credential_store_locked`) instead of waiting for a prompt, and retries (W558). A client older than this change needs the relay restarted once after the unlock (`systemctl --user restart kdcube-problem-board-relay-*.service`, the operator approves).

**The alternative W258 has to choose between** is in that item's note: the person
types it (nothing stored, a person needed after every reboot), or the machine
holds a generated secret in a file only that user reads (unattended, protected
by file permissions, readable by that user's agents).

**Host agent** checks it, then installs the relay:

```bash
PB_PYTHON="$HOME/.kdcube/client-runtime/tools/problem-board/releases/current/venv/bin/python"
"$PB_PYTHON" -c 'import keyring; keyring.set_password("pb-probe","p","x"); print(keyring.get_password("pb-probe","p")=="x"); keyring.delete_password("pb-probe","p")'
pb relay-service install
pb relay-service status        # installed: true, running: true
pb source status               # command and relay report the same pinned source
systemd-analyze --user verify ~/.config/systemd/user/kdcube-problem-board-relay-*.service   # prints nothing
```

The first machine found a unit `pb` wrote with a quoted path, which systemd
refuses. A selected client source without that fix must be advanced through
step 13 first.

An existing host keeps its former `/opt` or `problem-board-venv` install until
the complete step 13 migration proof has passed for every configured target.

## 7. Give the host access to exactly the project's repositories

Runs on: the host (the revoke sheet), and GitHub in the operator's browser (deleting old deploy keys).

**First, the owners' GitHub keys** (W371, [GitHub access for
agents](repo:app-ecosystem/products/project-board/docs/github.md)). An agent
pushes and opens pull requests under its owner's GitHub key: the owner links
GitHub once on their My Card for the project (the **Set up GitHub** button on
their card on the board), its Card holds **Use GitHub**, and the deployment's
GitHub App is installed on the repository. `pb worker connect-project` then
clones over HTTPS with `pb worker git-credential` as each clone's helper, and
`pb worker gh` runs gh with the key for one command. This host stores no
GitHub token and needs no deploy key.

**Deploy keys are not used for GitHub (W942).** The operator, 2026-10-23: "only
github defined in operator card should be used", "this is prohibited to use
on-host github". `pb worker connect-project` reaches a GitHub repository only
with the owner's key. For a repository the key does not serve yet, the row is
`needs_key` with the key's answer (`github_key`), and the owner links GitHub
on their Card with access to it. No deploy key is made, nothing is cloned or
fetched over SSH, and no `deploykey` remote is added. `pb worker push` refuses
an SSH or deploy-key remote (`push_remote_not_owner_key`). When the key is
unavailable or refused, the push fails and names the key's answer, and nothing
is pushed another way. The deploy-key steps below remain only to find and
revoke keys made earlier.

**Deploy keys made earlier: the revoke sheet.** Before W942, this step and
`pb worker connect-project` gave each repository a deploy key
(`~/.ssh/deploy_<alias>`) and a `Host github-<alias>` SSH block. Nothing uses
or makes them now. The operator decides when they are removed from GitHub
(W942: the operator asked to wait). **Revoking stays with the script below**:
it prints one **REVOKE** block for every key this procedure made on this host,
and changes nothing.

**Host agent**, as the Linux user that runs the agents, with the host id:

```bash
# Input: HOST_ID. Prints a REVOKE block for every deploy key this procedure made on this host; makes nothing.
( set -euo pipefail
KEYS=${KEYS:-$HOME/.ssh}; SSH_CONFIG=${SSH_CONFIG:-$KEYS/config}
recorded_repo() {  # the owner/repo a key was made for, from its comment
  awk '$NF ~ /\// {print $NF; exit}' "$1"
}
revoke_block() {  # $1 alias  $2 repository  $3 public key  $4 files to remove  $5 why
  printf '\n### REVOKE %s (%s)\n\nRepository: %s\nFind the deploy key titled "%s agents" with fingerprint %s and delete it.\nThen, on the host: rm %s. The clone stays for any unfinished work.\n' \
    "$1" "$5" "$2" "$HOST_ID" "$(ssh-keygen -lf "$3" </dev/null | awk '{print $2}')" "$4"
}
found=0
for pub in "$KEYS"/deploy_*.pub; do
  [ -e "$pub" ] || continue
  found=1; alias=${pub##*/deploy_}; alias=${alias%.pub}
  revoke_block "$alias" "$(recorded_repo "$pub" || true)" "$pub" \
    "${pub%.pub} $pub, and the \"Host github-$alias\" block in $SSH_CONFIG" "deploy keys are not used (W942)"
done
for pub in "$KEYS"/retired_*.pub; do
  [ -e "$pub" ] || continue
  found=1; alias=${pub##*/retired_}; alias=${alias%_*}
  revoke_block "$alias" "$(recorded_repo "$pub" || true)" "$pub" "${pub%.pub} $pub" "a retired key"
done
[ "$found" = 1 ] || echo "ok: this host has no deploy key made by this procedure"
)
```

**Operator**, for each **REVOKE** block, when they decide to remove the keys:
open that repository's deploy keys, delete the key with that title and
fingerprint, and tell the host agent, which then removes the key files and the
SSH block the block names. Deleting a deploy key needs admin rights on that
repository.

[Worked example: a machine's sheet](#worked-example-a-machines-sheet).

### GitHub CLI for pull requests and review verdicts

With the owner's GitHub key (the default, [GitHub access for
agents](repo:app-ecosystem/products/project-board/docs/github.md)), `gh` only has to be installed (step 1). Nothing is signed in:
an agent runs `pb worker gh -- pr create ...` (or any gh command), and pb gives
gh the owner's short-lived token for that one command. Pushes go through
`pb worker git-credential`, which connect-project names as each clone's helper
for github.com; commits carry the owner's My Card email.

**When Connection Hub cannot answer** (a platform rebuild, an outage), the key
is unavailable, not refused. `pb worker push -- <git push arguments>` then
fails, names the key's answer ("owner key unavailable (http_502)"), and
pushes nothing another way; push again when the key answers. A refusal
(not_attending, card_denies, github_not_linked…) is the same: no deploy key,
no other account.

**No other GitHub route (W942).** An owner with no GitHub key links GitHub on
their Card. Nothing is signed in to `gh` on the machine, and no machine
account or deploy key stands in for the owner's key.

**Card edits on the coordinator's machine.** A project-file edit made on the
card is applied by the coordinator's relay, a service (a LaunchAgent on macOS,
a systemd user service on Linux) that opted in with
`pb host configure --add-control-kind project.file.edit` (step 3). It pushes the
edit's branch and opens its pull request under the **coordinator owner's**
GitHub key, so `gh` must be installed on that machine. The relay finds gh by its
absolute path (its own `PATH` is short); when gh is missing, the edit's result
says so with the places it looked, the coordinator's notice says why, and the
coordinator opens the pull request from the compare link. No token is stored
in a file for the service.

## 8. Give each agent its own workspace

Runs on: the host.

**Host agent** creates one empty folder per agent, readable only by this user.
It clones nothing: the agent clones the project's repositories into it when it
attends the project (step 12), at the branch the card declares, so a workspace
always matches the card it serves.

```bash
mkdir -p ~/.kdcube/pb/workspaces && chmod 700 ~/.kdcube/pb/workspaces
for a in <alias-1> <alias-2>; do mkdir -p ~/.kdcube/pb/workspaces/$a; done
sudo -u <another-user> ls ~/.kdcube/pb/workspaces     # must be: Permission denied
```

Step 9 starts each agent in its workspace, and enrolling there (step 10)
records that folder as its workspace, which is how `pb worker context` tells
it where its clones go.

## 9. Start the agent sessions

Runs on: the host.

**Before the first Claude Code session**, the user's Claude Code settings
(`~/.claude/settings.json`) carry a status line and two hooks, and
`pb procedure install --target claude-code` (step 3) merges them in:
- `statusLine` runs `<pb> worker limit-state`, and the `StopFailure` hook runs
  `<pb> worker limit-state --source stop-failure` for every error that stops a
  session: together they report each agent's usage, and the moment a limit
  stops it, to its card;
- the `Stop` hook runs `<pb> worker stop-guard`, which keeps the session's
  watch running.

`<pb>` is the `pb` that ran the install, by its full path: the launcher on
`PATH` when it runs that same `pb`, otherwise the program itself. Run the
install through `pb`; an install that cannot name its `pb` is refused before it
writes anything. The install keeps every other key and hook, adds only what is
missing, and changes nothing on a second run. Older Project Board entries are
brought up to date in place: a bare `pb` becomes the full path, and a
`StopFailure` matcher that covered only `rate_limit` gains every other stopping
error. Before it writes, it keeps a copy of the file as
`settings.json.bak-<UTC time>`, readable by the user only, and never replaces
an earlier backup. A `settings.json` that is a symlink stays one,
and the file it points to is what changes. Its output, `claude_code_settings`,
lists what it added, updated and kept, the backup and the undo command (copy the
backup back). A status line that already runs something else is left as it is
and named in `notes`, with how to pipe its JSON into `pb worker limit-state`. A
settings file that is not valid JSON is refused and left unchanged. The first-run reference
("Claude Code Says When It Is Out Of Tokens") explains what each entry
reports. Without them the card says `limit not reported` (as the second
host's did until 2026-09-24). Codex agents need none of this.

A person enrolling one agent by hand follows [enroll an agent](./enroll-an-agent.md): three steps, the commands, nothing more. The start scripts below hold the same commands for a host that runs its agents unattended.

**Host agent**, first, one start script per agent in `~/.local/bin`, holding its
workspace and every flag, so a start or a restart is one short command and no
flag can be lost. A long command pasted by hand gets cut: on 2026-09-24 both
agents of the second host came back without `--add-dir ~/.kdcube`,
`--dangerously-skip-permissions` and `--disallowedTools AskUserQuestion` (W304
finding 35). For a Claude Code agent:

```bash
cat > ~/.local/bin/start-<agent-name> <<'EOF'
#!/usr/bin/env bash
# Start <agent-name>, or resume it with: start-<agent-name> <session-id>
set -eu
W="$HOME/.kdcube/pb/workspaces/<alias>"
export PATH="$HOME/.local/node/bin:$HOME/.local/bin:$PATH"
cd "$W"
exec env CLAUDE_CONFIG_DIR="$HOME/.claude-accounts/<account-email>" claude ${1:+--resume "$1"} \
  --add-dir "$W" --add-dir "$HOME/.kdcube" \
  --dangerously-skip-permissions --disallowedTools AskUserQuestion
EOF
chmod 700 ~/.local/bin/start-<agent-name>
```

`<account-email>` is the email of the Claude account the agent works under;
its folder holds that account's own login, settings, skill and history, and
receives the skill and settings with `pb procedure install --target claude-code
--claude-config-dir "$HOME/.claude-accounts/<account-email>"`. The first start
in a new folder asks for that login once. Agents that already run on the
host's global login move with [Claude account profiles](./claude-account-profiles.md).

Then one detached `tmux` session per agent, named after the agent and started
from its direct login (step 1) so the session has the user-session environment.
The session reads and writes its workspace and the user's `pb` state, and does
not stop to ask for each command, because nobody approves each command:

```bash
tmux -u new-session -d -s <agent-name> -x 220 -y 55 "bash -lc 'start-<agent-name>; exec bash'"
```

- **tmux carries Claude Code's display and mouse.** Claude Code draws with UTF-8 box characters and turns
  on mouse reporting. In a `screen` session started without UTF-8, the operator
  who attaches sees `?` for every box character, and each mouse movement arrives
  in the agent's input box as text (`94;29M97;29M...`). tmux carries both, and
  Claude Code recognizes it. `-u` forces UTF-8.
- **`~/.local/bin` comes before `/usr/local/bin`.** The inert `pb` launcher from
  step 2 lives in `~/.local/bin`. A host set up before that launcher existed may
  still have an old `/usr/local/bin/pb`, and the session must not run it.
- **`--disallowedTools AskUserQuestion`** keeps the session from stopping on an
  interactive question nobody watches: a worker asks the operator by board mail,
  which reaches them wherever they are.

**Host agent**, for a Codex worker, writes its start script with this exact
unattended worker mode, and starts it the same way:

```bash
cat > ~/.local/bin/start-<agent-name> <<'EOF'
#!/usr/bin/env bash
# Start <agent-name>, or resume it with: start-<agent-name> <session-id>
set -eu
W="$HOME/.kdcube/pb/workspaces/<alias>"
export PATH="$HOME/.local/node/bin:$HOME/.local/bin:$PATH"
exec codex ${1:+resume "$1"} -C "$W" \
  --sandbox danger-full-access --ask-for-approval never --search \
  --add-dir "$HOME/.kdcube"
EOF
chmod 700 ~/.local/bin/start-<agent-name>
tmux -u new-session -d -s <agent-name> -x 220 -y 55 "bash -lc 'start-<agent-name>; exec bash'"
```

- `--sandbox danger-full-access` gives Codex the rights of the dedicated host
  user. That user's home permissions, the workspace layout from step 8, and the
  owner's GitHub key through `pb worker git-credential` (step 7) are the boundary. Codex can write Git
  metadata, create commits, run project tools, and reach the approved remotes.
- `--ask-for-approval never` is the unattended worker policy: an approval prompt
  cannot hold the session while nobody is attached, and a failed command returns
  to the model. The agent still routes project and operator decisions through
  Project Board. `on-request` is the attended diagnostic mode: substitute it
  while a person remains attached to answer Codex prompts. Sandbox access and
  approval prompts are separate, so `on-request` can prompt even with
  `danger-full-access`.
- **One host user is one boundary.** Agents run by the same Linux user share
  its rights: with Claude Code's bypass mode or Codex's `danger-full-access`,
  each can read and write the other agents' workspaces and the user's `pb`
  state. A workspace per agent keeps their work apart by order, not by
  permission. Agents that must not reach each other run as separate host users.
- `-C` names the agent's primary workspace. `--add-dir ~/.kdcube` names the
  worker and relay state the session uses. `--search` enables live web search.
- The login relay wakes this exact session with
  `codex queue --thread <session-id>`. The queued turn tells the model to run
  `pb worker receive --wake-id <wake-id>`; a Codex worker runs no
  `pb worker watch` process.

Claude Code's first start in bypass mode shows a one-time warning. Accepting it
is the **operator's** decision. The host agent then selects **Yes, I accept**
and confirms:

```bash
tmux send-keys -t <agent-name> Down      # to "Yes, I accept"
tmux send-keys -t <agent-name> Enter
```

A prompt is sent the same way, text and Enter as two separate commands, because
a return inside the text is read as part of it. Keep the prompt short, and read
the answer with `capture-pane`:

```bash
tmux send-keys -t <agent-name> -l 'Use the problem-board-worker skill. Join Project Board as <agent-name>.'
tmux send-keys -t <agent-name> Enter
tmux capture-pane -p -t <agent-name> | tail -40      # read what it shows
```

Once running, a Claude Code agent keeps its own inbox watch and the guard
prompt that renews it, as the skill's Start Or Resume step 5 and its
claude-code-wake reference say. It needs no host step.

A session keeps its board identity only when resumed with its id, from the same
workspace and with the same flags; the start script keeps both. Resuming is also
how a session started with older flags gets the current ones:
`start-<agent-name> <session-id>`. The session id is the agent's stable name
without its `claude-code-` or `codex-` prefix. A new session is a new worker.

### Watch or talk to an agent

**Operator**, from their own terminal, at any time:

```bash
ssh -t -i ~/.ssh/<key> <user>@<host> tmux attach -t <agent-name>
```

It looks like the operator's own Claude Code session. To leave without stopping
the agent, press **Ctrl-b**, then **d**. The agent keeps running after the
terminal closes, and so does its board mail.

The terminal is the agent's chat box. Typed text sits in the input box, where
the operator can still edit or clear it, and reaches the agent on **Enter** as
its next message, the same as typing into their own session. Three keys act at
once, without Enter:

- **Esc** stops the agent's current response.
- **Ctrl-c** interrupts the current step. A second **Ctrl-c** exits Claude Code,
  and the tmux session is left at a shell prompt.
- **Shift-Tab** switches the session's permission mode.

Board mail remains the channel for work, because the other agents and the
project record see it.

**Restart an agent**, for example after a Claude Code or Codex update: attach,
type `/exit`, and at the shell prompt that the tmux session is left at, run
`start-<agent-name> <session-id>`. A Claude Code agent comes back without its
inbox watch and guard prompt, so send it the skill's Start Or Resume prompt:

```bash
tmux send-keys -t <agent-name> -l 'Use the problem-board-worker skill. Resume as <agent-name>: Start Or Resume.'
tmux send-keys -t <agent-name> Enter
```

A Codex agent needs no prompt: its login relay wakes it.

To watch without taking the keyboard, attach read-only:
`tmux attach -r -t <agent-name>`. Read-only fits a tab left open to follow an
agent, a second person watching, or a screen share: no key pressed there reaches
the agent, so a stray Ctrl-c cannot stop it. Leave it the same way, **Ctrl-b**,
then **d**.

### Move an existing agent to the workspace root under `~/.kdcube`

Runs on: the host, in a runtime window the coordinator announces. One agent at
a time; the others keep working.

Agents set up before 2026-09-25 work in `~/workspaces/<name>`. The operator
ruled on 2026-09-26 (W304 decision 4) that they all move to
`~/.kdcube/pb/workspaces/<alias>`, **conversations included**, so each agent
keeps its board identity, its conversation and its memory.

What a move breaks, found by a dry run on a Linux host on 2026-09-26:

- **Claude Code files memory per start folder.** An agent resumed from the new
  folder still finds its conversation by id, but it reads and writes memory in
  a new, empty folder and loses what it remembered. The fix is to rename the
  agent's folder under `~/.claude/projects` to the new folder's name **before
  the first start from the new folder**. A start from the new folder before
  the rename creates the new name first, and the rename then lands inside it.
  The folder's name is the path with every character that is not a letter
  or a digit replaced by `-`, including `@`, `_`, `+` and `.` (checked on
  Claude Code 2.1.283), so an alias such as `agent-two@host-two` is covered.
- **Git worktrees record absolute paths both ways.** A worktree inside the
  moved folder points to its checkout, and the checkout lists the worktree. A
  checkout that stays behind (a host's shared checkouts in `~/src`) keeps a
  stale entry that a later `git worktree prune` there would drop.
- **A Python virtual environment inside the folder does not move:** its
  scripts name the old path. Rebuild it after the move.

Codex keeps its sessions by id under `~/.codex`, not per folder: `codex resume
<session-id>` from anywhere finds it, and `-C` gives it the new folder.

**Before:** the agent is idle, its work is committed or pushed, and the host
agent notes its session id (the stable name without `claude-code-` or
`codex-`) and its old folder. Then the agent's session stops: attach, type
`/exit`, and leave the tmux session at its shell prompt.

**Host agent**, the move, with the values filled in:

```bash
( set -eu
A=<agent-name> ALIAS=<alias> SID=<session-id>
OLD=<absolute old folder, e.g. /home/<user>/workspaces/space001>
NEW=$HOME/.kdcube/pb/workspaces/$ALIAS
enc() { printf '%s' "$1" | sed 's#[^A-Za-z0-9]#-#g'; }   # Claude Code's folder name for a path
PO=$HOME/.claude/projects/$(enc "$OLD"); PN=$HOME/.claude/projects/$(enc "$NEW")
[ -d "$OLD" ] || { echo "STOP: $OLD is not a folder"; exit 1; }
[ ! -e "$NEW" ] && [ ! -e "$PN" ] || { echo "STOP: $NEW or $PN already exists"; exit 1; }
mkdir -p "$(dirname "$NEW")"
[ ! -d "$PO" ] || mv "$PO" "$PN"      # Claude Code only: conversation and memory
mv "$OLD" "$NEW"
find "$NEW" -maxdepth 3 -name .git -type f | while read -r f; do
  wt=${f%/.git}; gd=$(sed -n 's/^gitdir: //p' "$f"); gd=${gd/#"$OLD"/"$NEW"}
  git -C "${gd%/worktrees/*}" worktree repair "$wt"
done
echo "moved $A: $OLD -> $NEW" )
```

Then:

1. `pb host show` lists `$HOME/.kdcube/pb/workspaces` under `allowed_roots`.
   When it does not, `pb host configure --add-allow-root
   $HOME/.kdcube/pb/workspaces`, then restart the relay (a coordinated
   runtime action, already inside this window).
2. Write the agent's start script (step 9) with `<alias>`, and start it from
   the tmux session's shell prompt: `start-<agent-name> <session-id>`.
3. A Claude Code agent gets the Start Or Resume prompt; every agent gets this
   message, by tmux or board mail:
   `Your workspace moved from <old folder> to <new folder>. Re-read your
   journal and project facts, update any path you remember under the old
   folder (memory, pb worker workspace registrations), and rebuild any virtual
   environment that lived there.`
4. **Check:** in the agent's session, `pwd` names the new folder. A Claude Code
   agent can still quote a line from its memory. `git worktree list` in each
   repository shows no entry under the old folder and none `prunable`. The
   agent's card is live, and it answers the coordinator's mail.

**Rollback**, the same steps reversed, with the session stopped again. Use it
when the check fails and a fix is not obvious in the window:

```bash
( set -eu
# same A, ALIAS, SID, OLD, NEW, enc, PO, PN as above
[ ! -e "$OLD" ] && [ ! -e "$PO" ] || { echo "STOP: $OLD or $PO already exists"; exit 1; }
mv "$NEW" "$OLD"
[ ! -d "$PN" ] || mv "$PN" "$PO"
find "$OLD" -maxdepth 3 -name .git -type f | while read -r f; do
  wt=${f%/.git}; gd=$(sed -n 's/^gitdir: //p' "$f"); gd=${gd/#"$NEW"/"$OLD"}
  git -C "${gd%/worktrees/*}" worktree repair "$wt"
done
echo "rolled back $A: $NEW -> $OLD" )
```

Then start the agent the old way, from `<old folder>` with the flags of step 9,
and tell the coordinator what failed.

**Dry run, a Linux host, 2026-09-26** (throwaway Claude Code session, Claude Code
2.1.283, deleted afterwards): a session told a word to remember in
`~/workspaces/dryrun-move` saved it to memory. Resumed from the moved folder
**without** the rename, it knew the word from its conversation, but its
memory was left behind. **With** the rename before the first start, it knew
the word and found it in its memory, and nothing was recreated under the old
name. The rollback restored both. The worktree loop repaired a shared checkout
outside the folder, a clone inside it, and a nested `worktrees/` folder.

## 10. Enroll each agent

Runs on: the host, inside each agent session.

**Joining, enrolling and authorizing are three steps, and only the last creates
a Card.** The host joins the deployment in steps 3 and 6: `pb setup` records the
target and host, and the relay service runs for that user. That creates no
Card. Enrolling (this step) records one agent session as a channel of this
host's relay. That creates no Card either. Authorizing (step 11) creates the
agent's Card in Connection Hub and stores its credential on the host.

The prompt above makes the agent run `pb worker whoami` and
`pb worker listen --alias <agent-name>`. It reports its stable worker name and
the profile to authorize (`problem-board-claude-…`).
Renaming an agent later is the same command with the new name, run in its
session. The relay carries the new name on the next heartbeat, and the later
of it and the operator's pencil on the agent's card wins. `pb worker whoami`
shows the board's answer as `board_alias`.

Identify an agent session with three facts: its coding provider, the provider
account reported by its host, and its native resumable session ID. Problem
Board routing remains stable on provider plus session ID. The board keeps the
first host-login reading it takes for a session. That reading is inferred, not
proof of the account the session runs under. When the host later logs in to
another account, the board shows that login beside the first reading as a
mismatch, and the worker keeps its mail and assignment address. A session
started after the new login is a new worker, whose first reading is the new
login.

**Known gap (W271):** until it is authorized, an enrolled worker exists only in
this host's relay configuration: the board does not list it, and only a shell on
the host can retire it (`pb worker detach --runtime-kind claude-code
--runtime-session-id <id>`).

## 11. Authorize each agent from the operator's browser

Runs on: the host (the command) and any device with a browser (the operator's approval).

The host has no browser, so it uses device login: the command prints a
verification URL and a short code, and the operator approves on any device. The
host opens no listener and needs no tunnel. The private device code never leaves
the host, and the credential is stored there.

**Host agent**, in its direct login on the host, one agent at a time:

```bash
pb worker authorize <profile> --device
```

`<profile>` here is the agent's Card authorization profile, which `pb worker
listen` reported, not a KDCube profile (`--profile`, step 3).

The command reads the public provider-account description from the coding
runtime at authorization time, when the runtime publishes one, and includes it
in the client's bounded registration metadata. There is no account flag to type
and no token to copy. Before approving, the consent page shows **Provider
account**, the provider name and account identifier, and **Reported by the
host**. This is identification metadata; the operator's Card choices establish
access. If the runtime does not publish an account description, the command
prints `Provider account not reported` and continues authorization without it.

It prints the URL and the code. The **operator** opens the URL on any device,
signs in, enters the code, and approves the presented worker authority. A first
Card labels the client as a **Project Board worker** and checks the
descriptor's standard worker operations. To create a coordinator Card instead,
use:

```bash
pb worker authorize <profile> --device --coordinator
```

That consent labels the client as a **Project Board coordinator** and checks
the whole current Project Board operation catalog. The worker profile is an
explicit operation list; a newly published operation does not reach workers
until the descriptor adds it. The coordinator profile is the whole catalog;
newly published operations appear in its next first or replacement consent.

The minimum a worker's Card needs to use the relay is the grant `work:relay`
(the channel: publish and heartbeat, pull and settle controls, route mail,
report assignments), and `work:observe` to read the plan it works on
(`project.plan.item`, `plan.notes.list`). A worker without them is refused
when it calls those operations. The standard worker grants and operations are already selected;
leave them as shown for a normal worker, or uncheck authority that this worker
must not have. Do not copy rows from another worker's Card. Provider accounts
and their claims are separate, default-closed choices and stay unselected
unless this worker actually needs one. The command on the host then completes.

The role option applies only when no Card exists or when the operator asks for
a replacement. With an existing profile, ordinary `pb worker authorize
<profile> --device` reconnects that exact Card and keeps its authority. Adding
`--coordinator` does not rewrite it. To deliberately replace an existing Card
with the coordinator proposal, revoke and replace it in one explicit action:

```bash
pb worker authorize <profile> --device --replace-card --coordinator
```

Replacement revokes the old Card before the new consent. The descriptor-owned
profile and consent enforcement are defined in [Delegated Access
Cards](repo:app-ecosystem/docs/connection-hub/package/delegated-cards.md#descriptor-owned-authorization-profiles).
Device mode is defined in [first-time setup](first-time-setup.md#authorize-a-headless-host).

Device login is proven live end to end (two Claude Code agents on a headless
Linux host, 2026-09-24 and 2026-09-25) and is built into the server: there is no
callback, port, tunnel or manual step to fall back to (operator, 2026-09-30). When
it fails, the refusal code names the cause; report it to the operator and change
nothing.

An **existing** worker profile needs no fallback for device reauthorization.
Its recorded OAuth client may have been registered for browser authorization
before the device grant was available; the Connection Hub release with the W414
Card continuity check gives every existing public native client the device grant, so
`pb worker authorize <profile> --device` works for it, with the same client and
Card. Against a server without that release, the command stops with
`work_relay_device_client_unauthorized`, naming the missing release: have the
operator deploy it, then run the same command again. Do not use callback ports,
tunnels, `--replace-card` or a different client for this case. `invalid_client`
means the client could not be resolved; `invalid_target` or `invalid_scope`
points to request binding instead.

Device login re-authorizes an existing Card only with **continuity proof** from
the machine asking: the command sends the Card's last refresh token from this
profile's stored credential, and the server accepts it only when it belongs to
one of that Card's credential families for the same client, revoked families
included (operator, 2026-09-30). A link and code alone never re-authorize an
existing Card. A new Card still rests on the approver checking the request, so
approve only a request you started. A host whose stored credential is gone, or holds another
Card's, stops with `work_relay_card_continuity_required` and the Card is not
touched: report it to the operator, who decides whether the Card is replaced.

**After the operator approves**, tell each agent in its tmux session, since
an agent waiting for approval does not check its inbox yet:

```bash
tmux send-keys -t <agent-name> -l 'Authorized. Follow Start Or Resume of the problem-board-worker skill.'
tmux send-keys -t <agent-name> Enter
```

## 12. Attend the project and prove each agent works in the team

Runs on: the board in the operator's browser (attendance and the operator's
messages), the operator's phone (Telegram), and the host (`pb worker inspect`,
the relay log).

The **operator** adds each agent to the project, on the board's **Team >
Agents > Add agent** (the agent and its role) or on the agent's pool card,
**Add to project**. When the role is coordinator, the operator then presses
**Fit to role** on that agent's row in **Team > Agents** and checks that its
receipts say applied: it gives its Card the coordinator shape (the coordinator profile). Within seconds the agent's
host holds the project's record: its team and the repositories set on the
project card. **The agent** then sets up its workspace from that record with
`pb worker connect-project` (first-run's Part 2), as the worker procedure's
project-workspace reference says:
- it reads `pb worker context --project-ref <project>` once `project_on_this_host` is true;
- it clones each listed repository, the journal repository (role `journal`) included, into `<workspace>/<alias>` at its declared branch;
- it fetches and fast-forwards any it already has;
- it tells the operator by name about any it cannot reach.

A GitHub repository it cannot reach is one the owner's GitHub key does not
serve yet: `pb worker connect-project` names it as `needs_key` with the key's
answer, the owner links GitHub on their Card with access to it, and the agent
runs the command again (W942: no deploy key). Nothing is cloned by hand: a
clone the card does not list is not the project's.

The workspace holding each listed alias at its branch is the proof. Joining a
project sends the agent one welcome notice: the project, its role, the
coordinator by stable name, the team, the journal home, and what to do first
(read `pb worker context`, clone the listed repositories, then tell the
coordinator it is ready, naming anything it could not reach). That ready
message is the first sign it is working. Then the coordinator and the
operator prove, **one check at a time**, that each agent communicates on every
channel and knows it is part of the team. The coordinator proposes each check,
the operator approves it, and the result is shown before the next one. An agent
that fails a check does not get work until the failure is understood.

| # | Check | How | Proves |
|---|---|---|---|
| 1 | Wakes without help | The agent runs its own notification path (one `pb worker watch` for Claude Code, the relay's native queue for Codex). The coordinator sends one message, and the agent picks it up with nobody typing into its terminal. | the notification path on this host |
| 2 | Coordinator round trip | The coordinator sends a question. The agent receives, replies, and settles the lease. | worker-to-worker mail |
| 3 | Operator inbox to agent | The operator sends the agent a message from the board. The agent replies to the operator. | the operator channel, inbound |
| 4 | Knows it is in the team | The agent states its stable worker name, its owner and logged-in account, the project it attends, the coordinator, the other agents, and one fact from the project facts page that `pb worker context` names. | it reads the team from the board, not from being told |
| 5 | Talks to a teammate | The agent sends one short message to another agent, and gets a reply. | agent to agent across machines |
| 6 | Writes to the operator's inbox | The agent sends the operator ordinary mail (kind `update`). It appears in the board inbox and not on Telegram. | agent to operator, inbox |
| 7 | Reaches the operator's phone, and back | The agent sends the operator kind `question`. It appears in the board inbox and in the operator's private chat with the bot, in the project's channel (a topic named after the project, created on the first post), with a link to the board. The operator answers with Telegram's Reply on that post, and the answer reaches the agent as operator mail correlated to the question. | the urgent channel, both ways |

When check 2 fails because the agent refused the coordinator's mail, the
board sends the coordinator a delivery-failure notice, and the host's relay log
has a line. Both name `receiver_policy_peer_denied` and the setting
`receiver_policy.allowed_peer_workers`:
the host's teammate list was narrowed without the coordinator, or the host was
set up before `*` became the default and holds an empty list. The host owner
sets it (`pb host configure --allow-peer-worker`) and restarts the relay,
and the check runs again. On 2026-09-24 this refused every project mail to the
first agent onboarded on the second host.

On the host, `pb worker inspect` shows each channel open, and the relay log
shows `event=opened` for each worker. Record the results in the project journal
and the machine and agents in the project facts page.

**Only then is work assigned**, still one step at a time with the operator: a
first small, self-contained item per agent, reviewed by an agent on another
machine, merged and deployed by the coordinator as usual.

## 13. Update The Selected Client Source

Runs on: the host.

The operator or coordinator first agrees the source move with every agent on
this host because either action restarts the shared relay. The installed
worker procedure's `references/runtime-actions.md`, found with
`pb procedure show`, owns that agreement and verification sequence.

### Migrate An Existing Host

Keep the checkout and the former `problem-board-venv` available while creating
the first complete release environment. This same sequence migrates a checkout
launcher, a user-owned permanent venv, or the older root-owned `/opt` install:

```bash
APP_REPOSITORY=/home/<user>/src/app-ecosystem
APP_COMMIT=<approved-full-commit>
APP_EXPORT=$(mktemp -d)
test "$(git -C "$APP_REPOSITORY" rev-parse "$APP_COMMIT^{commit}")" = "$APP_COMMIT"
git -C "$APP_REPOSITORY" archive "$APP_COMMIT" | tar -x -C "$APP_EXPORT"
python3 \
  "$APP_EXPORT/products/project-board/packages/project-board/scripts/install_from_source.py" \
  --source-root "$APP_EXPORT"
"$HOME/.local/bin/pb" --version
"$HOME/.local/bin/pb" relay-service install
"$HOME/.local/bin/pb" source use-code \
  --repository "$APP_REPOSITORY" --ref "$APP_COMMIT" \
  --expect "$APP_COMMIT"
"$HOME/.local/bin/pb" source status
```

The installer builds and smokes the candidate before moving
`releases/current`. Reinstalling the relay service once changes its definition
from the former interpreter to the stable
`releases/current/venv/bin/python` path. `source use-code` then records both
reviewed commit as one release and performs the verified restart.

Install the procedure, then inspect status for every target configured on this
host:

```bash
pb procedure install --target claude-code --target codex
pb procedure verify
pb source status
```

The migration is complete when each target reports all of these facts:

1. `launcher.version` is `2`, `launcher.current` is true, and
   `active_release.environment.path` is below
   `releases/<release-id>/venv`;
2. `relay.program_arguments[0]` ends in
   `releases/current/venv/bin/python`;
3. every target receipt and every `relay.startup_record.source` name the same
   snapshot release ID, the App Ecosystem commit, and all four package tree IDs;
4. `pb procedure verify` succeeds through `~/.local/bin/pb`.

After every target passes those checks, no launcher or relay service consumes
`~/.kdcube/client-runtime/tools/problem-board-venv`; the host agent may delete
that directory. A root-owned `/opt` install and `/usr/local/bin/pb` may likewise
be removed by the machine administrator. A failed build leaves the former
client and relay running. A failed relay startup restores the previous current
release, every target receipt, the launcher, and every installed relay.

### Restart the agent sessions after an update

A running session keeps the procedure it loaded. After `pb procedure install`,
each Claude Code agent restarts to load the new revision: in its tmux session,
`/exit`, then the full resume command from step 9 with its session id, from the
same workspace. It keeps its board identity.

## 14. Retire an agent or the host

Runs on: the host, and GitHub in the operator's browser (deleting old deploy keys).

- One agent: `pb worker detach` in its session, then end its tmux session
  (`tmux kill-session -t <agent-name>`). Detaching only disables the agent's
  relay channel row; `pb host configure --remove-disabled-channels` removes
  every disabled row once no session on the host needs one back.
- The host's access to one repository: remove it from the project card. Any deploy
  key made for it earlier is on step 7's revoke sheet.
- The whole host: detach every agent, run `pb relay-service uninstall` and
  remove the inert `pb` launcher and its release store for each
  user, delete any old deploy keys on GitHub (step 7's revoke sheet), then remove that user's Project Board
  state after retaining any required audit material.

## Worked example: a machine's sheet

The revoke sheet from step 7 for a machine that had deploy keys (host
`host-two`, user `agent-user`). Fingerprints only: the keys themselves stay
in `~/.ssh` on that host until the operator deletes them on GitHub and the
host agent removes the files. The fingerprint below is a placeholder.

### REVOKE applications (deploy keys are not used (W942))

Repository: kdcube/applications
Find the deploy key titled "host-two agents" with fingerprint SHA256:<fingerprint> and delete it.
Then, on the host: rm ~/.ssh/deploy_applications ~/.ssh/deploy_applications.pub, and the "Host github-applications" block in ~/.ssh/config. The clone stays for any unfinished work.
