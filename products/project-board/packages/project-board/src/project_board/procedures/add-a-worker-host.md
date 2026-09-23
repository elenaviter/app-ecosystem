---
id: app-ecosystem.project-board.procedure.add-a-worker-host
title: Add A Worker Host
summary: Step by step, for a person or for an agent acting for them, to turn a remote Linux machine on a private network into a host that runs Problem Board agent sessions with access to exactly the repositories they work on.
tags: [procedure, problem-board, setup, relay, worker, linux, headless]
keywords: [worker host, tailnet, headless, deploy key, pinned client, systemd relay, keyring, ssh tunnel, bypass permissions, private code]
see_also:
  - ./first-time-setup.md
  - ./operator.md
  - https://github.com/kdcube/applications/blob/main/playground/domain-solution/apps/problem-board@1-0/docs/add-a-machine-README.md
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
[add a machine](https://github.com/kdcube/applications/blob/main/playground/domain-solution/apps/problem-board@1-0/docs/add-a-machine-README.md).

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
| 11 | approve each agent's Card in a browser |
| 12 | add the agents to the project |

Everything else the host agent does over its own SSH session. The operator's
own terminal on the host is needed only for the password in step 6.

**Every operator step states, in the step, why a person is required and what it
commits them to afterwards.** An operator step exists because of an identity or
a secret, never because a command is awkward. Step 6 leaves the operator holding
a password, so it says so before they type one.

## What you end up with

```text
new host
└── /home/<user>/                      the Linux user that runs the agents
    ├── .local/bin/pb                  guarded project-board launcher
    ├── .kdcube/client-runtime/tools/problem-board-venv/   isolated bootstrap
    ├── .kdcube/          (700)        selectors, snapshots, relay state, logs, mailboxes
    ├── .ssh/deploy_<repo>{,.pub}      one deploy key per repository
    ├── .config/systemd/user/kdcube-problem-board-relay-*.service
    └── workspaces/       (700)
        ├── <workspace-1>/             one agent, its own clones
        └── <workspace-2>/             another agent, its own clones
```

**The client source is selected once per target.** The released `project-board`
bootstrap reads one selector shared by ordinary `pb` commands and the relay.
It selects either the exact installed release or an immutable code snapshot
exported from reviewed App Ecosystem and KDCube commits. Agent workspaces are
never a runtime source. The selector moves only by
[step 13](#13-update-the-selected-client-source).

**Private state stays private.** A shared machine has other users, and a home
directory is often readable by a shared group. The selected code snapshots,
workspaces, and `pb` state are readable only by the user who runs those agents.
Every step that writes code checks this.

## 0. Decide before starting

The host agent presents these as one proposal, and the operator approves it
before anything changes:

| decision | example | why it matters |
|---|---|---|
| host id and label | `spark1`, "spark1 (Linux, headless)" | the machine's name on the board. "The agents on a host agree before a relay restart" is per host id. |
| Linux user that runs the agents | `lena` | owns the keys, the relay and the credential store |
| **repositories the agents may work on** | see the table below | the most important decision: each gets a deploy key with write access and a clone in every workspace. The agents reach nothing else through Problem Board. |
| workspaces, one per agent | `~/workspaces/space001`, `space002` | each agent edits only its own clones |
| agent names | `claude-ops@spark1`, `claude-app@spark1` | display names on the board. The board addresses a worker by a stable generated name. |
| coding agent account | the account whose subscription the agents use | step 5 logs in with it. Every session of that Linux user shares it. |
| who approves the agents' Cards | the project's operator | the KDCube user the agents act for. Until a project can have more than one operator (W260), it is the project's operator. |

The repository table, used by steps 2, 7 and 8:

| local name | GitHub `owner/repo` | private | what the agents do there |
|---|---|---|---|
| `applications` | `kdcube/applications` | yes | Problem Board and the other apps |
| `app-ecosystem` | `elenaviter/app-ecosystem` | no | Connection Hub and foundation packages |
| `kdcube-ai-app` | `kdcube/kdcube` | no | KDCube platform and SDK |

## 1. Give the host agent access to the host

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
getent passwd | awk -F: '$3>=1000 && $3<60000 {print $1}'   # other users on the machine
stat -c '%A %G %n' ~                                         # who can read the home directory
```

Needed: Python 3.11 or newer, git, a user-session environment, sudo. Note the
other users and the home directory's group: they decide how much the user home,
client state, and workspaces must lock down.

## 2. Install The Pinned `pb` Bootstrap For The Agent User

**Host agent**, logged in as the user who will run the agents, installs the
client family from clean exports of the exact App Ecosystem and KDCube commits
approved by the operator:

```bash
APP_REPOSITORY=/home/<user>/workspaces/app-ecosystem
APP_COMMIT=<approved-full-commit>
APP_EXPORT=$(mktemp -d)
KDCUBE_REPOSITORY=/home/<user>/workspaces/kdcube-ai-app
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

The source installer owns the isolated environment and guarded user launcher.
Its one resolver invocation binds `project-board`, `app-foundation`,
`service-foundation`, `connection-hub`, and `connection-hub-cli` to the App
Ecosystem export and `kdcube-cli` to the KDCube export; package indexes provide
only third-party dependencies. Do not use
`sudo`, a checkout launcher, or an editable install. Repeat the install for
another login user rather than sharing one credential-bearing runtime between
users.

## 3. Configure `pb` for the user that runs the agents

**Host agent**, as that user:

```bash
pb procedure install --target claude-code --target codex
pb setup \
  --target-id <target> \
  --endpoint <problem_board MCP endpoint of the deployment> \
  --tenant <tenant> --platform-project <project> \
  --host-id <host-id> --host-label "<label>" \
  --allow-root /home/<user>/workspaces
chmod 700 ~/.kdcube
pb source use-code \
  --repository /home/<user>/workspaces/app-ecosystem \
  --ref <approved-full-commit> \
  --expect <approved-full-commit> \
  --kdcube-repository /home/<user>/workspaces/kdcube-ai-app \
  --kdcube-ref <approved-full-kdcube-commit> \
  --expect-kdcube <approved-full-kdcube-commit>
pb source status
pb relay --once          # zero workers, success
```

The endpoint is the one the first machine uses (its `relay.json`,
`target.endpoint`). It is the deployment's public address and is never written
into shared documents.

**Known gap (W261):** `pb` creates `~/.kdcube` readable by the home directory's
group, which let another user list the mailboxes and relay configuration on the
first machine. `chmod 700` closes it until W261 lands.

## 4. Keep the user's services running

**Host agent:**

```bash
sudo loginctl enable-linger <user>
loginctl show-user <user> -p Linger      # Linger=yes
```

Without it, the relay stops when the last SSH session of that user closes.

## 5. Install the coding agent and log it in

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

## 6. Give the relay a credential store, then install it

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
PB_PYTHON="$(pipx environment --value PIPX_LOCAL_VENVS)/project-board/bin/python"
"$PB_PYTHON" -c 'import keyring; keyring.set_password("pb-probe","p","x"); print(keyring.get_password("pb-probe","p")=="x"); keyring.delete_password("pb-probe","p")'
pb relay-service install
pb relay-service status        # installed: true, running: true
pb source status               # command and relay report the same pinned source
systemd-analyze --user verify ~/.config/systemd/user/kdcube-problem-board-relay-*.service   # prints nothing
```

The first machine found a unit `pb` wrote with a quoted path, which systemd
refuses. A selected client source without that fix must be advanced through
step 13 first.

## 7. Give the host access to exactly the approved repositories

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

**Host agent**, one detached `screen` per agent, started from its direct login
(step 1) so the session has the user-session environment. The session reads and
writes its workspace and the user's `pb` state, and does not stop to ask for
each command, because nobody watches its screen:

```bash
screen -dmS <agent-name> bash -c 'cd ~/workspaces/<workspace> && \
  export PATH=$HOME/.local/node/bin:/usr/local/bin:$PATH && \
  claude --add-dir ~/workspaces/<workspace> --add-dir ~/.kdcube --dangerously-skip-permissions; exec bash'
```

For Codex, the equivalent is
`codex -C ~/workspaces/<workspace> -s danger-full-access`.

The first start in bypass mode shows a one-time warning. Accepting it is the
**operator's** decision. The host agent then selects **Yes, I accept** and
confirms:

```bash
screen -S <agent-name> -X stuff "$(printf '\033[B')"   # down, to "Yes, I accept"
screen -S <agent-name> -X stuff '^M'                     # Enter
```

A prompt is sent the same way, text and Enter as two separate commands, because
a return inside pasted text is read as part of the text:

```bash
screen -S <agent-name> -X stuff 'Use the problem-board-worker skill. Join Problem Board as <agent-name>.'
screen -S <agent-name> -X stuff '^M'
screen -S <agent-name> -X hardcopy /tmp/<agent-name>.txt    # read what it shows
```

A session keeps its board identity only when resumed with its id, from the same
workspace: `claude --resume <session-id>`. A new session is a new worker.

## 10. Enroll each agent

The prompt above makes the agent run `pb worker whoami` and
`pb worker listen --alias <agent-name>`. It reports its stable worker name and
the profile to authorize (`problem-board-claude-…`).

**Known gap (W271):** until it is authorized, an enrolled worker exists only in
this host's relay configuration: the board does not list it, and only a shell on
the host can retire it (`pb worker detach --runtime-kind claude-code
--runtime-session-id <id>`).

## 11. Authorize each agent from the operator's browser

The host has no browser, so the approval runs in the operator's browser and the
answer reaches the host through an SSH tunnel. The authorization code is useless
without the verifier, which never leaves the host, and the credential is stored
on the host.

**Operator**, on their own machine, leaves a tunnel open:

```bash
ssh -i ~/.ssh/<key> -N -L 18765:127.0.0.1:18765 <user>@<host>
```

**Host agent**, in its direct login on the host, one agent at a time:

```bash
pb worker authorize <profile> --no-open --callback-port 18765
```

It prints a URL. The **operator** opens it and approves the presented worker
authority. The standard worker grants and operations are already selected;
leave them as shown for a normal worker, or uncheck authority that this worker
must not have. Do not copy rows from another worker's Card. Provider accounts
and their claims are separate, default-closed choices and stay unselected
unless this worker actually needs one. The command on the host then completes.
Close the tunnel after the last agent.

Device login (W257) replaces the tunnel when it lands: the host prints a short
code and the operator enters it on any device.

## 12. Attend the project and prove the round trip

The **operator** adds each agent to the project. The coordinator (or the
operator) sends each agent a message, and the agent receives, replies and
settles it. On the host, `pb worker inspect` shows each channel open, and the
relay log shows `event=opened` for each worker.

## 13. Update The Selected Client Source

The operator or coordinator first agrees the source move with every agent on
this host because either action restarts the shared relay. The installed
worker procedure's `references/runtime-actions.md`, found with
`pb procedure show`, owns that agreement and verification sequence.

### Migrate A Host That Still Runs `pb` From A Checkout

Keep the checkout at its working revision until its relay interpreter can load
the source package. Read `program_arguments[0]` from `pb relay-service status`;
it identifies the environment the installer must update:

```bash
APP_REPOSITORY=/home/<user>/workspaces/app-ecosystem
APP_COMMIT=<approved-full-commit>
APP_EXPORT=$(mktemp -d)
KDCUBE_REPOSITORY=/home/<user>/workspaces/kdcube-ai-app
KDCUBE_COMMIT=<approved-full-commit>
KDCUBE_EXPORT=$(mktemp -d)
test "$(git -C "$APP_REPOSITORY" rev-parse "$APP_COMMIT^{commit}")" = "$APP_COMMIT"
test "$(git -C "$KDCUBE_REPOSITORY" rev-parse "$KDCUBE_COMMIT^{commit}")" = "$KDCUBE_COMMIT"
git -C "$APP_REPOSITORY" archive "$APP_COMMIT" | tar -x -C "$APP_EXPORT"
git -C "$KDCUBE_REPOSITORY" archive "$KDCUBE_COMMIT" | tar -x -C "$KDCUBE_EXPORT"
PB_VENV=$(dirname "$(dirname "<relay-python>")")
python3 \
  "$APP_EXPORT/products/project-board/packages/project-board/scripts/install_from_source.py" \
  --source-root "$APP_EXPORT" \
  --kdcube-source-root "$KDCUBE_EXPORT" \
  --venv "$PB_VENV"
"<relay-python>" -c 'import json; from project_board.client.source_control import installed_release_source; print(json.dumps(installed_release_source(), sort_keys=True))'
"<relay-python>" -m project_board.client.entrypoint source use-code \
  --repository "$APP_REPOSITORY" --ref "$APP_COMMIT" \
  --expect "$APP_COMMIT" \
  --kdcube-repository "$KDCUBE_REPOSITORY" --kdcube-ref "$KDCUBE_COMMIT" \
  --expect-kdcube "$KDCUBE_COMMIT"
"<relay-python>" -m project_board.client.entrypoint source status
```

The verification line proves the installed bootstrap is isolated and reports
its package version. `source use-code` selects both reviewed commits as one
release and restarts the relay. Confirm snapshot mode, the release ID, both
commits, all six tree IDs, and a matching relay startup record. Then install
the procedure and only then
fast-forward the shared checkout to the carve that imports `project_board`:

```bash
pb procedure install --target claude-code --target codex
pb procedure verify
pb source status
```

`pb source status` separately reports the released bootstrap, selected source,
and running relay source. All three facts remain visible after the terminal
that performed the change exits. A failed relay verification restores the
previous selector and source.

## 14. Retire an agent or the host

- One agent: `pb worker detach` in its session, then end its screen.
- The host's access to one repository: delete its deploy key in that repository.
- The whole host: detach every agent, run `pb relay-service uninstall` and
  remove the guarded `pb` launcher and its isolated client environment for each
  user, delete the deploy keys on GitHub, then remove that user's Problem Board
  state after retaining any required audit material.

## Worked example: spark1, 2026-09-22

The operator sheet from step 7 for the first machine (host `spark1`, user
`lena`). Public keys only: the private halves stay in `~/.ssh` on spark1.

### applications

Page: https://github.com/kdcube/applications/settings/keys
Title: spark1 agents
Allow write access: yes
Key:

    ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDyqUwnfhZsW0owdt1t6dqeU36I27aks2iuS5DnI/ZDL spark1 deploy key: applications

### app-ecosystem

Page: https://github.com/elenaviter/app-ecosystem/settings/keys
Title: spark1 agents
Allow write access: yes
Key:

    ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAzcyOq6rmbwmcldadYYQ52Qf2zvmlislDzmQ/DOFjGk spark1 deploy key: app-ecosystem

### kdcube-ai-app

Page: https://github.com/kdcube/kdcube/settings/keys
Title: spark1 agents
Allow write access: yes
Key:

    ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIENLyr8v3pFmuBI+rYjV5i8g2GrWav2uJ/uRd1Z0DYET spark1 deploy key: kdcube-ai-app
