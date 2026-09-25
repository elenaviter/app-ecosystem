---
id: app-ecosystem.project-board.procedure.add-a-worker-host
title: Add A Worker Host
summary: Step by step, for a person or for an agent acting for them, to turn a remote Linux machine on a private network into a host that runs Problem Board agent sessions with access to exactly the repositories they work on.
tags: [procedure, problem-board, setup, relay, worker, linux, headless]
keywords: [worker host, tailnet, headless, deploy key, pinned client, systemd relay, keyring, device login, bypass permissions, private code]
see_also:
  - ./first-time-setup.md
  - ./operator.md
  - repo:app-ecosystem/docs/project-board/add-a-machine.md
  - repo:app-ecosystem/docs/connection-hub/package/delegated-cards.md#descriptor-owned-authorization-profiles
---

# Add A Worker Host

This procedure adds a machine to an existing Problem Board deployment: a remote
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
[add a machine](repo:app-ecosystem/docs/project-board/add-a-machine.md).

## Who does what

- **Operator**: the person who owns the Problem Board project.
- **Host agent**: an agent with an SSH session on the new host, acting for the
  operator. A person can do every host-agent step too.

The operator does only what needs their identity or a secret:

| step | operator action |
|---|---|
| 1 | give the host agent an SSH login to the machine |
| 5 | log the coding agent in with the account it should use |
| 6 | type the credential store password once per boot (until W258) |
| 7 | add each deploy key on GitHub |
| 9 | accept bypass mode for the agent sessions |
| 11 | approve each agent's Card, with a code, in a browser on any device |
| 12 | add the agents to the project |

Everything else the host agent does over its own SSH session. The operator's
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
    ├── src/app-ecosystem, src/kdcube          public clones the client is built from, read only
    ├── .ssh/deploy_<repo>{,.pub}      one deploy key per repository
    ├── .config/systemd/user/kdcube-problem-board-relay-*.service
    └── workspaces/       (700)
        ├── <workspace-1>/             one agent, its own clones
        └── <workspace-2>/             another agent, its own clones
```

**The client release is selected once per host.** `releases/current` selects
one complete environment used by `~/.local/bin/pb` and every relay definition
on the host. Each target retains its own source-action receipt, so status can
show which approved released version or App Ecosystem plus KDCube commits were
applied for that target. Agent workspaces are build workspaces rather than
runtime import paths. The host release moves only by
[step 13](#13-update-the-selected-client-source).

**What the install brings, and what it does not.** Steps 2 to 6 install, for
one Linux user: the inert `pb` launcher, the current client release environment
(`project-board` with its Connection Hub client and foundation packages), the
worker procedure package for Claude Code and Codex, and the user relay service.
They do not install Problem Board itself, which is an app on the KDCube
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
| host id and label | `spark1`, "spark1 (Linux, headless)" | the machine's name on the board. "The agents on a host agree before a relay restart" is per host id. |
| Linux user that runs the agents | `lena` | owns the keys, the relay and the credential store |
| **repositories the agents may work on** | see the table below | the most important decision: each gets a deploy key with write access and a clone in every workspace. The agents reach nothing else through Problem Board. |
| workspaces, one per agent | `~/workspaces/space001`, `space002` | each agent edits only its own clones |
| agent names | `claude-ops@spark1`, `claude-app@spark1` | display names on the board. The board addresses a worker by a stable generated name. |
| runtime of each agent | `claude-code` for both, or one of each | an agent session is described by its runtime, provider account, and session ID. Its stable worker address remains runtime plus session ID; an account change is recorded and reported. One host can run Claude Code and Codex agents side by side, each in its own workspace. Needing another runtime means adding another agent. |
| account per runtime | the Claude account for Claude Code agents, the OpenAI account for Codex agents | step 5 logs each runtime in once. Every agent of that runtime under the same Linux user shares its login and its usage. |
| who approves the agents' Cards | the project's operator | the KDCube user the agents act for. Until a project can have more than one operator (W260), it is the project's operator. |
| `tmux` on the host | installed by whoever administers the machine | step 9 runs each agent in it. It is a system package, so a user-level install cannot provide it. |
| **teammates who may write to these agents** | `*`, any agent that shares a project with them, or named workers such as the coordinator | the host's receiver policy (`receiver_policy.allowed_peer_workers`), set in step 3. A new host accepts mail from no teammate until it is set, so the coordinator cannot reach the new agents. The board already lets only an agent in a shared project address them, so `*` means "my project teammates". It grants no access to anything: it decides whose mail reaches these agents. The operator's own messages reach them either way. |

The repository table, used by steps 2, 7 and 8:

| local name | GitHub `owner/repo` | private | what the agents do there |
|---|---|---|---|
| `applications` | `kdcube/applications` | yes | Problem Board and the other apps |
| `app-ecosystem` | `elenaviter/app-ecosystem` | no | Connection Hub and foundation packages |
| `kdcube` | `kdcube/kdcube` | no | KDCube platform and SDK |

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
getent passwd | awk -F: '$3>=1000 && $3<60000 {print $1}'   # other users on the machine
stat -c '%A %G %n' ~                                         # who can read the home directory
```

Needed: Python 3.11 or newer, git, a user-session environment, sudo. Note the
other users and the home directory's group: they decide how much the user home,
client state, and workspaces must lock down.

## 2. Install The Pinned `pb` Bootstrap For The Agent User

Runs on: the host.

**Host agent**, logged in as the user who will run the agents, first clones the
two source repositories the client is built from. Both are public, and these
clones are the install source only, read and never edited: each agent gets its
own clones in step 8.

```bash
mkdir -p ~/src
[ -d ~/src/app-ecosystem ] || git clone -q https://github.com/elenaviter/app-ecosystem.git ~/src/app-ecosystem
[ -d ~/src/kdcube ] || git clone -q https://github.com/kdcube/kdcube.git ~/src/kdcube
git -C ~/src/app-ecosystem fetch -q origin && git -C ~/src/kdcube fetch -q origin
```

Then it installs the client family from clean exports of the exact App
Ecosystem and KDCube commits approved by the operator:

```bash
APP_REPOSITORY=/home/<user>/src/app-ecosystem
APP_COMMIT=<approved-full-commit>
APP_EXPORT=$(mktemp -d)
KDCUBE_REPOSITORY=/home/<user>/src/kdcube
KDCUBE_COMMIT=<approved-full-commit>
KDCUBE_EXPORT=$(mktemp -d)
test "$(git -C "$APP_REPOSITORY" rev-parse "$APP_COMMIT^{commit}")" = "$APP_COMMIT"
test "$(git -C "$KDCUBE_REPOSITORY" rev-parse "$KDCUBE_COMMIT^{commit}")" = "$KDCUBE_COMMIT"
git -C "$APP_REPOSITORY" archive "$APP_COMMIT" | tar -x -C "$APP_EXPORT"
git -C "$KDCUBE_REPOSITORY" archive "$KDCUBE_COMMIT" | tar -x -C "$KDCUBE_EXPORT"
python3 \
  "$APP_EXPORT/products/project-board/packages/project-board/scripts/install_from_source.py" \
  --source-root "$APP_EXPORT" \
  --kdcube-source-root "$KDCUBE_EXPORT"
```

The source installer calls the same release builder as later source switches.
It creates `releases/<release-id>/venv`, resolves and smokes the complete
candidate, atomically moves `releases/current`, and installs launcher version
2. Its one resolver invocation binds `project-board`, `app-foundation`,
`service-foundation`, `connection-hub`, and `connection-hub-cli` to the App
Ecosystem export and `kdcube-cli` to the KDCube export; package indexes provide
only third-party dependencies. Do not use
`sudo`, a checkout launcher, or an editable install. Repeat the install for
another login user rather than sharing one credential-bearing runtime between
users.

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
  --allow-root /home/<user>/workspaces
pb host configure --allow-peer-worker '<step 0 decision: * or one worker name per flag>'
chmod 700 ~/.kdcube
pb source use-code \
  --repository /home/<user>/src/app-ecosystem \
  --ref <approved-full-commit> \
  --expect <approved-full-commit> \
  --kdcube-repository /home/<user>/src/kdcube \
  --kdcube-ref <approved-full-kdcube-commit> \
  --expect-kdcube <approved-full-kdcube-commit>
pb source status
pb procedure install --target claude-code --target codex
pb procedure verify
pb relay --once          # zero workers, success
```

`--allow-peer-worker` sets the whole list of teammates who may write to these
agents, from step 0. A host set up before this setting existed has an empty
list and refuses every teammate's mail (dev-main until 2026-09-24): run the
same command there. Repeat the flag for several names, and `pb host show`
prints the result. Run it again later to change the list, then restart the
relay (a coordinated runtime action) so it reads the new policy.

The procedure is installed after `pb source use-code`, because the selected
client carries the revision. `pb procedure verify` proves the installed
procedure matches it. A stale or unverifiable package is a defect in this
path, recorded and fixed before the walk goes on, never skipped: on a host that
joined the team earlier the package was eight revisions behind.

The endpoint is the one the first machine uses (its `relay.json`,
`target.endpoint`). It is the deployment's public address and is never written
into shared documents.

**Known gap (W261):** `pb` creates `~/.kdcube` readable by the home directory's
group, which let another user list the mailboxes and relay configuration on the
first machine. `chmod 700` closes it until W261 lands.

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

**Operator** (or the person whose account the Codex agents use): log in once, in
an SSH session as that user. Run `codex login` and complete the sign-in it
prints, in a browser on any machine. The login lands in `~/.codex/auth.json`, and
every Codex session of that Linux user uses it.

**Why a person:** each login is an account and its usage. Every agent of that
runtime under this Linux user works under it, which is why step 0 names the
account per runtime.

The host also keeps the public account description used to identify the
worker: Claude Code publishes it as `oauthAccount` in `~/.claude.json`; Codex
publishes an account ID and public identity claims in `~/.codex/auth.json`.
`pb worker authorize` reads only the account ID, email, and organization from
that local runtime state. Credential and token values stay in their native
files and never enter Problem Board, Connection Hub client metadata, command
arguments, or logs. A runtime may use an API key or otherwise omit this public
account description. In that case authorization continues without the
identification metadata and reports `Provider account not reported`.

## 6. Give the relay a credential store, then install it

Runs on: the host.

**Known gap (W258):** a headless machine's credential store (Secret Service)
exists but cannot create its store over SSH: the first write fails with
`Prompt dismissed`. Until W258 lands, the **operator** unlocks it once per boot,
in an SSH session of their own as that user, because the password is theirs. It
is typed, not echoed, and not stored:

```bash
read -rs P && printf %s "$P" | gnome-keyring-daemon --replace --unlock --components=secrets >/dev/null; unset P
```

The first run creates a store named `login` with that password.

**Tell the operator before they type it**: the password is theirs to keep, the
machine cannot remind them of it, they are asked again after every reboot of the
machine and only then, and if it is lost the store is deleted
(`~/.local/share/keyrings/login.keyring`), unlocked again with a new password,
and each agent is authorized again (step 11). Until this step is done, an agent
cannot finish authorizing, because it has nowhere to keep its credential.

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

## 7. Give the host access to exactly the approved repositories

Runs on: the host (keys, verification), and GitHub in the operator's browser (adding each key).

One **deploy key per repository**: an SSH key that one repository accepts for
itself. A personal key would reach every repository its owner can reach, and
removing it would cut off every machine using it. Deleting a deploy key revokes
exactly this host's access to exactly that repository.

**Host agent**, for each repository in the step 0 table:

```bash
ssh-keygen -q -t ed25519 -N "" -C "<host-id> deploy key: <local-name>" -f ~/.ssh/deploy_<local-name>
cat >> ~/.ssh/config <<'CONFIG'
Host github-<local-name>
  HostName github.com
  User git
  IdentityFile ~/.ssh/deploy_<local-name>
  IdentitiesOnly yes
CONFIG
chmod 600 ~/.ssh/config
```

Then it prints the **operator sheet**: for each repository, the page to open, the
title and the key to paste. Keep the step 0 table in `~/.kdcube/repositories.tsv`
(local name, `owner/repo`, one per line, tab-separated), and run:

```bash
while IFS=$'\t' read -r name repo; do
  printf '\n### %s\n\nPage: https://github.com/%s/settings/keys\nTitle: %s agents\nAllow write access: yes\nKey:\n\n    %s\n' \
    "$name" "$repo" "$(hostname -s)" "$(cat ~/.ssh/deploy_$name.pub)"
done < ~/.kdcube/repositories.tsv
```

**Operator**, for each block of the sheet: open the page, **Add deploy key**,
enter the title, paste the key line, tick **Allow write access** (the agents
push their work branches), **Add key**. Adding a deploy key needs admin rights on
that repository. Merging into the default branch stays governed by review.

**Host agent** verifies each:

```bash
while IFS=$'\t' read -r name repo; do
  printf '%s: ' "$name"; git ls-remote "github-$name:$repo.git" HEAD >/dev/null 2>&1 && echo ok || echo REFUSED
done < ~/.kdcube/repositories.tsv
```

Rules: one key per repository per host user. Another user on the machine who
runs agents gets their own keys. The private key never leaves the host. Public
keys are not secret: the sheet can be sent by any channel.

[Worked example: the first machine's sheet](#worked-example-spark1-2026-09-22).

## 8. Give each agent its own workspace and clones

Runs on: the host.

**Host agent:**

```bash
mkdir -p ~/workspaces && chmod 700 ~/workspaces
for w in <workspace-1> <workspace-2>; do
  mkdir -p ~/workspaces/$w
  while IFS=$'\t' read -r name repo; do
    [ -d ~/workspaces/$w/$name ] || git clone -q "github-$name:$repo.git" ~/workspaces/$w/$name
  done < ~/.kdcube/repositories.tsv
done
sudo -u <another-user> ls ~/workspaces     # must be: Permission denied
```

If step 7 is not finished yet, a public repository clones over HTTPS and a
private one from a bundle made on a machine that has it (`git bundle create
x.bundle main`, then `git clone x.bundle <name>` and
`git -C <name> checkout -B main origin/main`). A bundle carries everything that
machine has on `main`, including commits not yet pushed. Either way, point
`origin` at the alias so pushing works once the key is added:

```bash
git -C <name> remote set-url origin "github-<name>:<owner>/<repo>.git"
```

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
missing, and changes nothing on a second run. Older Problem Board entries are
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
reports. Without them the card says `limit not reported` (spark1 until
2026-09-24). Codex agents need none of this.

**Host agent**, one detached `tmux` session per agent, named after the agent and
started from its direct login (step 1) so the session has the user-session
environment. The session reads and writes its workspace and the user's `pb`
state, and does not stop to ask for each command, because nobody approves each command:

```bash
tmux -u new-session -d -s <agent-name> -x 220 -y 55 "bash -lc 'cd \$HOME/workspaces/<workspace> && \
  export PATH=\$HOME/.local/node/bin:\$HOME/.local/bin:\$PATH && \
  claude --add-dir \$HOME/workspaces/<workspace> --add-dir \$HOME/.kdcube --dangerously-skip-permissions \
    --disallowedTools AskUserQuestion; exec bash'"
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

A Codex agent starts in its own tmux session the same way, with `codex` in
place of `claude`. Which sandbox and approval flags a Codex worker should use is
being settled in W307, which records the two forms in use today. A Codex worker
is woken by the relay through its native queue, so it needs no `pb worker watch`.

The first start in bypass mode shows a one-time warning. Accepting it is the
**operator's** decision. The host agent then selects **Yes, I accept** and
confirms:

```bash
tmux send-keys -t <agent-name> Down      # to "Yes, I accept"
tmux send-keys -t <agent-name> Enter
```

A prompt is sent the same way, text and Enter as two separate commands, because
a return inside the text is read as part of it. Keep the prompt short, and read
the answer with `capture-pane`:

```bash
tmux send-keys -t <agent-name> -l 'Use the problem-board-worker skill. Join Problem Board as <agent-name>.'
tmux send-keys -t <agent-name> Enter
tmux capture-pane -p -t <agent-name> | tail -40      # read what it shows
```

Once running, a Claude Code agent keeps its own inbox watch and the guard
prompt that renews it, as the skill's Start Or Resume step 5 and its
claude-code-wake reference say. It needs no host step.

A session keeps its board identity only when resumed with its id, from the same
workspace and with the same flags. Resuming is also how a session started with
older flags gets the current ones:
`claude --resume <session-id> --add-dir ~/workspaces/<workspace> --add-dir ~/.kdcube --dangerously-skip-permissions --disallowedTools AskUserQuestion`.
A new session is a new worker.

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

To watch without taking the keyboard, attach read-only:
`tmux attach -r -t <agent-name>`. Read-only fits a tab left open to follow an
agent, a second person watching, or a screen share: no key pressed there reaches
the agent, so a stray Ctrl-c cannot stop it. Leave it the same way, **Ctrl-b**,
then **d**.

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

Identify an agent session with three facts: its coding provider, the provider
account reported by its host, and its native resumable session ID. Problem
Board routing remains stable on provider plus session ID, so changing provider
accounts does not create a replacement worker. The board records the new
provider account, warns the operator and that worker, and keeps the existing
mail and assignment address.

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
Card labels the client as a **Problem Board worker** and checks the
descriptor's standard worker operations. To create a coordinator Card instead,
use:

```bash
pb worker authorize <profile> --device --coordinator
```

That consent labels the client as a **Problem Board coordinator** and checks
the whole current Problem Board operation catalog. The worker profile is an
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

**Known gap: device login is not yet proven live end to end (W257).** Its live
run stopped at the operator approval on 2026-09-24. The first host that
completes step 11 with `--device` records it in W257. If device login fails, the
fallback is the callback through an SSH tunnel. The **operator** leaves the
tunnel open on their own machine:

```bash
ssh -i ~/.ssh/<key> -N -L 18765:127.0.0.1:18765 <user>@<host>
```

and the host agent runs the same command with `--no-open --callback-port 18765`
in place of `--device`. The operator opens the printed URL in their own browser,
and closes the tunnel after the last agent.

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

The **operator** adds each agent to the project. Within seconds the agent's
host holds the project's record: its team and the repositories set on the
project card. **The agent** then sets up its workspace from that record, as
the worker procedure's project-workspace reference says:
- it reads `pb worker context --project-ref <project>` once `project_on_this_host` is true;
- it clones each listed repository, the journal repository (role `journal`) included, into `<workspace>/<alias>` at its declared branch;
- it fetches and fast-forwards any it already has;
- it tells the operator by name about any it cannot reach.

The workspace holding each listed alias at its branch is the proof. Joining a
project sends the agent no welcome message yet (W304 finding 37, pending): the
coordinator's first message in check 2 is its first project mail. Then the coordinator and the
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
| 7 | Reaches the operator's phone | The agent sends the operator kind `question`. It reaches the operator's phone through Telegram (with a link to the board) and the board inbox. The operator answers from the board, and the answer reaches the agent. Telegram carries notifications one way: a reply typed in Telegram does not reach the agent. | the urgent channel, and the answer path |

When check 2 fails because the agent refused the coordinator's mail, the
board sends the coordinator a delivery-failure notice, and the host's relay log
has a line. Both name `receiver_policy_peer_denied` and the setting
`receiver_policy.allowed_peer_workers`:
step 3's peer setting is missing or does not name the coordinator. The host
owner sets it (`pb host configure --allow-peer-worker`) and restarts the relay,
and the check runs again. On 2026-09-24 this refused every project mail to the
first agent onboarded on spark1.

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
KDCUBE_REPOSITORY=/home/<user>/src/kdcube
KDCUBE_COMMIT=<approved-full-commit>
KDCUBE_EXPORT=$(mktemp -d)
test "$(git -C "$APP_REPOSITORY" rev-parse "$APP_COMMIT^{commit}")" = "$APP_COMMIT"
test "$(git -C "$KDCUBE_REPOSITORY" rev-parse "$KDCUBE_COMMIT^{commit}")" = "$KDCUBE_COMMIT"
git -C "$APP_REPOSITORY" archive "$APP_COMMIT" | tar -x -C "$APP_EXPORT"
git -C "$KDCUBE_REPOSITORY" archive "$KDCUBE_COMMIT" | tar -x -C "$KDCUBE_EXPORT"
python3 \
  "$APP_EXPORT/products/project-board/packages/project-board/scripts/install_from_source.py" \
  --source-root "$APP_EXPORT" \
  --kdcube-source-root "$KDCUBE_EXPORT"
"$HOME/.local/bin/pb" --version
"$HOME/.local/bin/pb" relay-service install
"$HOME/.local/bin/pb" source use-code \
  --repository "$APP_REPOSITORY" --ref "$APP_COMMIT" \
  --expect "$APP_COMMIT" \
  --kdcube-repository "$KDCUBE_REPOSITORY" --kdcube-ref "$KDCUBE_COMMIT" \
  --expect-kdcube "$KDCUBE_COMMIT"
"$HOME/.local/bin/pb" source status
```

The installer builds and smokes the candidate before moving
`releases/current`. Reinstalling the relay service once changes its definition
from the former interpreter to the stable
`releases/current/venv/bin/python` path. `source use-code` then records both
reviewed commits as one composite release and performs the verified restart.

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
3. the target receipt and `relay.startup_record.source` name the same snapshot
   release ID, both commits, and all six package tree IDs;
4. `pb procedure verify` succeeds through `~/.local/bin/pb`.

After every target passes those checks, no launcher or relay service consumes
`~/.kdcube/client-runtime/tools/problem-board-venv`; the host agent may delete
that directory. A root-owned `/opt` install and `/usr/local/bin/pb` may likewise
be removed by the machine administrator. A failed build leaves the former
client and relay running. A failed relay startup restores the previous current
release and target receipt.

### Restart the agent sessions after an update

A running session keeps the procedure it loaded. After `pb procedure install`,
each Claude Code agent restarts to load the new revision: in its tmux session,
`/exit`, then the full resume command from step 9 with its session id, from the
same workspace. It keeps its board identity.

## 14. Retire an agent or the host

Runs on: the host, and GitHub in the operator's browser (deleting deploy keys).

- One agent: `pb worker detach` in its session, then end its tmux session
  (`tmux kill-session -t <agent-name>`).
- The host's access to one repository: delete its deploy key in that repository.
- The whole host: detach every agent, run `pb relay-service uninstall` and
  remove the inert `pb` launcher and its release store for each
  user, delete the deploy keys on GitHub, then remove that user's Problem Board
  state after retaining any required audit material.

## Worked example: spark1, 2026-09-22

The operator sheet from step 7 for the first machine (host `spark1`, user
`lena`). Public keys only: the private halves stay in `~/.ssh` on spark1.
Each page is that repository's deploy-key settings,
`https://github.com/<owner>/<repository>/settings/keys`, which only its
administrators can open.

### applications

Title: spark1 agents
Allow write access: yes
Key:

    ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDyqUwnfhZsW0owdt1t6dqeU36I27aks2iuS5DnI/ZDL spark1 deploy key: applications

### app-ecosystem

Title: spark1 agents
Allow write access: yes
Key:

    ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAzcyOq6rmbwmcldadYYQ52Qf2zvmlislDzmQ/DOFjGk spark1 deploy key: app-ecosystem

### kdcube

Title: spark1 agents
Allow write access: yes
Key:

    ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIENLyr8v3pFmuBI+rYjV5i8g2GrWav2uJ/uRd1Z0DYET spark1 deploy key: kdcube-ai-app
