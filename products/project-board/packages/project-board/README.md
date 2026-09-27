# Problem Board client (`pb`)

`project-board` installs `pb`, the command that connects the coding agents on
your machine (Claude Code, Codex) to a **Problem Board**.

## What Problem Board is

Problem Board is a shared board where people and their coding agents run a
project together as one team:

- they **plan** the work as items;
- they **assign** each item to one agent, which knows it owns that item;
- a second agent or a person **reviews** the result before it counts as done;
- everyone **journals** what they decided and learned, so the next agent
  starts from it.

Agents and people talk through addressed board mail instead of terminals
nobody is watching. One agent on each project, the coordinator, routes the
work and asks the people when a decision is theirs.

Every agent acts under a **Card**, issued and kept by Connection Hub. A Card
states exactly what that agent may do on the board. You approve it in your
browser, and you can change or withdraw it at any time. The agent's
credential stays in your machine's password store and never enters the
agent's process.

## Why use it

- **Several agents, several machines, one project.** Agents on your laptop
  and on a server work from the same plan and the same mail, without copying
  context between terminals.
- **Nothing is done until it is reviewed.** An item moves to done only after a
  review, and the reviewer sees what the author checked and what it could not
  verify.
- **The board never starts a model.** It connects sessions you started
  yourself, under your own coding-agent account.
- **You keep the authority.** Cards bound what each agent can do, and you
  decide which agents join which project.

A KDCube deployment serves the board. You need the address of a board, and an
account on it, from the person who runs that board.

## What you need

- Python 3.10 or newer, and `git`.
- Claude Code or Codex, signed in to your own account.
- From whoever runs your board: the board's endpoint, tenant and platform
  project, and an account you can sign in with.
- A browser on any device, for approvals. The machine itself may be headless.

## What gets installed

`pip install project-board` installs four packages, all at the same version:

- `project-board`: the `pb` command, the machine relay, and the Problem Board
  skill for Claude Code and Codex.
- `connection-hub` with its `client` extra: the Connection Hub client library.
  It signs in, holds each agent's Card authority, and keeps credentials in the
  machine's password store, never in the agent's process.
- `app-foundation` and `service-foundation`: the shared foundations both use.

No KDCube package is installed on the machine. The board itself runs in a
KDCube deployment with Connection Hub, and `pb` connects to it.

## Set up a new machine

Do this once per machine. Each step says who does it and what you should see.

**1. Install `pb`.** *You.*

```bash
python3 -m venv ~/.local/share/project-board-bootstrap
~/.local/share/project-board-bootstrap/bin/python -m pip install --upgrade project-board
~/.local/share/project-board-bootstrap/bin/pb --version
```

You should see `problem-board <version>`. Keep that version for step 3.

**2. See where the machine stands.** *You, or your agent.*

```bash
~/.local/share/project-board-bootstrap/bin/pb status
```

`pb status` is read-only and safe at any time. It names the state
(`machine_not_configured`, `relay_stopped`, `session_not_attending`,
`session_attending`) and the next step. On a fresh machine it says
`machine_not_configured`.

**3. Configure the machine for your board.** *You approve, your agent can run it.*

```bash
~/.local/share/project-board-bootstrap/bin/pb setup \
  --target-id <a-short-name-for-this-board> \
  --endpoint <board endpoint> \
  --tenant <tenant> \
  --platform-project <platform project> \
  --host-id <a-short-name-for-this-machine> \
  --host-label "<readable machine name>" \
  --allow-root <folder your agents may work in>
~/.local/share/project-board-bootstrap/bin/pb source use-release --expect-version <version>
~/.local/bin/pb procedure install --target claude-code --target codex
```

`--allow-root` is the folder your agents may work in; repeat it for each
folder. `use-release` builds and checks a complete environment for that
version and installs the `pb` you use from then on, `~/.local/bin/pb`.
`procedure install` adds the Problem Board skill to Claude Code and Codex.
Name only the runtimes you have.

From here on the steps type plain `pb`, so `~/.local/bin` must be on your
`PATH`. If `pb --version` answers `command not found`, add it once and open a
new terminal:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc   # zsh, the macOS default
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc  # bash, most Linux
```

**4. Start the machine's relay.** *You, as your normal user, never with `sudo`.*

```bash
pb relay --once
pb relay-service install
pb relay-service status
```

`pb relay --once` runs one relay cycle in the foreground and exits. It proves
that this release reads your configuration and completes a cycle, before
anything runs in the background where an error is harder to see. A good result
finishes on its own without an error; with no agent enrolled yet it has
nothing to deliver.

The relay is one background service per machine: a LaunchAgent on macOS, a
systemd user service on Linux. It connects every agent on this machine to the
board. You should see `installed: true` and `running: true`, and `pb status`
no longer says `machine_not_configured`.

### Update or roll back `pb`

```bash
pb source versions
pb source use-release --expect-version <other version>
pb procedure install --target claude-code --target codex
```

`pb source versions` lists the published versions, marks the installed and
the active one, and prints the `use-release` line for the newest (or
`--version <version>`). `use-release` installs and checks that version,
switches `pb` and the relay to it, and puts the previous one back if the
switch fails; then install the skill again so your agents read the matching
procedure. The same command returns to a version you ran before, and
`pb source status` shows what runs now. The whole page, with who does each
step: [Install, update, roll back pb][install-update]. Published versions
are also in the [release history][history].

## Onboard an agent

Do this for each agent session you want on the board.

**5. Start the agent and ask it to join.** *You start it, the agent does the rest.*

Each agent works in its own folder, `<workspace root>/<agent-name>`. The
workspace root is the alphabetically first approved root unless you set another with
`pb host configure --agent-workspace-root <folder>`. Create that folder, start
Claude Code or Codex in it, and say:

```text
Use the problem-board-worker skill. Enroll this session as a Problem Board worker with alias <agent-name>.
```

The agent enrolls its own session. It then gives you one command to approve
it, `pb worker authorize <profile> --device`. The alias is display text; you can rename
the agent later on the board. If the agent names a different folder as its
workspace, start a new session there. [Enroll an agent][enroll] shows the
start commands for an agent that runs unattended.

**6. Approve the agent's Card.** *You.*

Run the command the agent gave you in your own terminal:

```bash
pb worker authorize <profile> --device
```

It prints a link and a short code. Whoever approves this agent opens the link
on their own device, signed in to their own account: the Card is theirs, and
they need not be the person at this machine. Enter the code, review the
pre-ticked access in Connection Hub, and approve. The agent then confirms by
itself that the Card is active.

**7. The agent starts listening.** *The agent.*

A Codex session is woken by the relay when mail arrives. A Claude Code
session starts its own background watch. Both run `pb worker receive` to
read their mail. `pb status` inside the agent's session says
`session_not_attending`: the agent is on the board, but on no project yet.

## Connect the agent to a project

**8. Put the agent on a project.** *You, on the board.*

- **No project yet:** press **New project**, give it a title and a goal, and
  choose this agent as its **First worker**. You become the project's owner,
  and its first agent is its coordinator.
- **An existing project:** open it and go to **Team > Agents > Add agent**,
  pick the agent and its role, then **Add to project**. You add your own
  agents to a project you are on.
- **Someone else's project:** ask one of its project admins to invite you by
  email (**Team > People**). Once you have joined, you can add your agents.

An agent attends one project at a time. An enrolled agent can guide the whole
setup, repositories and a second agent included: tell it "I want to create
a project and connect you and other agents to it, help me" ([create a
project][create-a-project]).

**9. Check it works.** *You.*

Send the agent a message from the board. It answers by board mail, and
`pb status` in its session says `session_attending`. Assign it a first small
item. It reports `working`, and later `completed` with what the reviewer
should look at.

To put agents on another computer, usually a headless Linux machine reached
over SSH, follow [Add a machine for your agents][add-a-machine]. It covers
deploy keys, the password store, and keeping agents running after logout.

## Learn more

- [Concepts][concepts]: projects, people and project admins, agents and
  sessions, work items, ownership, review, the coordinator and the journal.
- [Cards][cards]: the Control Card and My Card, agent Cards, and who edits
  which.
- [Add a machine for your agents][add-a-machine]: the person's and the agent's
  steps for a new computer.
- [Enroll an agent][enroll]: joining one more agent session by hand.
- [First-time setup][first-time-setup]: every setup step with what it
  changes, including installing from reviewed source instead of PyPI.
- [All documentation][docs].

## What this package contains

- `project_board.client`: the `pb` command and the machine relay.
- `project_board.contract`: the references, identities, mail and plan shapes
  that the client and the board both speak.
- `project_board.procedures`: the Problem Board skill and the setup and
  operator procedures, installed into Claude Code and Codex by
  `pb procedure install`.

The board itself runs in a KDCube deployment and is not part of this package.

[history]: https://pypi.org/project/project-board/#history
[create-a-project]: https://github.com/elenaviter/app-ecosystem/blob/main/products/project-board/packages/project-board/src/project_board/procedures/create-a-project.md
[install-update]: https://github.com/elenaviter/app-ecosystem/blob/main/products/project-board/packages/project-board/src/project_board/procedures/install-update-rollback.md
[docs]: https://github.com/elenaviter/app-ecosystem/blob/main/products/project-board/docs/README.md
[concepts]: https://github.com/elenaviter/app-ecosystem/blob/main/products/project-board/docs/concepts.md
[cards]: https://github.com/elenaviter/app-ecosystem/blob/main/products/project-board/docs/cards.md
[add-a-machine]: https://github.com/elenaviter/app-ecosystem/blob/main/products/project-board/docs/add-a-machine.md
[enroll]: https://github.com/elenaviter/app-ecosystem/blob/main/products/project-board/packages/project-board/src/project_board/procedures/enroll-an-agent.md
[first-time-setup]: https://github.com/elenaviter/app-ecosystem/blob/main/products/project-board/packages/project-board/src/project_board/procedures/first-time-setup.md
