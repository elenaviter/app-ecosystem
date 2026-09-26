---
id: project-board-add-a-machine
title: Add A Machine For Your Agents
summary: What you do, and what you hand your agent, to put Problem Board agents on another computer.
tags: [project-board, setup, worker host, user guide]
keywords: [add a machine, deploy key, authorize agent, headless, tailscale]
see_also:
  - ./README.md
  - repo:app-ecosystem/products/project-board/packages/project-board/src/project_board/procedures/add-a-worker-host.md
  - repo:app-ecosystem/products/project-board/packages/project-board/src/project_board/procedures/first-time-setup.md
---

# Add A Machine For Your Agents

Put agents on another computer: a Linux machine you reach over SSH, usually
without a screen. Your agent does the setup; you make six decisions and do four
things only you can do. Twenty minutes, most of it waiting.

Each of your steps below says **why it needs you** and **what it means
afterwards**. One of them (step 4) gives you a password to keep, so have your
password manager open.

Your setup agent follows
[add a worker host](repo:app-ecosystem/products/project-board/packages/project-board/src/project_board/procedures/add-a-worker-host.md). You do not need to read
it.

## Who does what

The whole setup, in the order it happens. **You** are the person who owns the
project. **Your agent** is the one helping you set up, for example your
coordinator. "Either" means a person can do that step instead of the agent.
Numbers in the first column are the steps of
[add a worker host](repo:app-ecosystem/products/project-board/packages/project-board/src/project_board/procedures/add-a-worker-host.md).

| Step | Who | What happens | Why that person |
|---|---|---|---|
| 0. Agree the plan | **You** decide, your agent proposes | One proposal: the machine's name on the board, the Linux user, **the project the agents join**, one workspace per agent, the **agent names**, the coding-agent account, **who may write to your agents**, and worker or coordinator Cards. Nothing changes before you approve it. | These are your choices. The project card's repositories are everything the agents can reach. |
| 1. Access to the machine | **You** | Give your agent an SSH key and the user the agents run as. | Only you hold that access. |
| 1. Check the machine | Either | Python, git, sudo, other users, home folder permissions, and `tmux`. | Routine. |
| 1. Install `tmux` if missing | **The machine's administrator** | `apt install tmux` or `dnf install tmux`. | A system package needs admin rights. |
| 2 to 3. Install and configure `pb` | Either | The client from the approved commits, the same release as your other machines, and the worker procedure. | Routine. |
| 4. Keep the agents running after logout | **The machine's administrator** | `sudo loginctl enable-linger` for the agents' user, so the relay keeps running when no one is logged in. | A system setting needs admin rights. |
| 5. Install the coding agent | Either | Node, then Claude Code and, when an agent uses it, Codex, in the user's home folder. | Routine. |
| 5. Log each runtime in | **You** (or the account owner) | [Log the coding agent in](#3-log-the-coding-agent-in): Claude Code through its printed browser flow; Codex by device code, with an SSH-forwarded browser callback as the fallback. | It is your account. |
| 6. Unlock the password store | **You** | [Unlock the password store](#4-unlock-the-machines-password-store-and-keep-that-password). Again after every reboot. | The password is yours to keep. |
| 6. Install the relay | Either | The service that connects this machine's agents to the board. | Routine. |
| After 6. Remove an old `/opt` install | **The machine's administrator** | Only on a machine set up before the user installer, once the relay runs from the user install: the root-owned environment under `/opt` and its `/usr/local/bin/pb`. | Removing root-owned files needs admin rights. |
| 7. Make the deploy keys | Your agent, once an agent has joined (step 12) | Compares the machine's keys with the project card: a sheet of keys to add, and of keys to delete for repositories no longer on the card. | Routine. |
| 7. Add or delete the keys on GitHub | **You** | [Add the deploy keys](#2-add-the-deploy-keys-on-github), and delete the ones the sheet lists. | Only a repository admin can grant or revoke access. |
| 8. Workspaces | Either | One empty folder per agent, under the host's agent workspace root (`<root>/<agent alias>`; set it with `pb host configure --agent-workspace-root <path>`, a folder inside an approved work root; unset, the alphabetically first approved work root). An agent started anywhere else still gets that folder from `pb worker context`, never the folder its session started in. Each agent clones the project's repositories into it when it joins (step 12). | Routine. |
| 9. Usage and watch settings | Your agent | The machine reports each agent's usage and the moment a limit stops it. Installing the procedure sets this up in the machine's Claude Code settings, keeps a copy of the previous settings, and changes nothing else. The relay wakes Codex through that session's native queue. | Routine. Without usage reporting the card says "limit not reported". |
| 9. Start the agents | Your agent | One start script per agent, holding its folder and every flag, and one `tmux` session per agent, named after it. | Routine. |
| 9. Approve unattended command mode | **You** decide, your agent applies it | Claude Code accepts its one-time bypass warning. Codex uses `--ask-for-approval never`; `on-request` is the attended mode where a person answers prompts. | It is your risk decision. The machine user and repository deploy keys remain the boundary. |
| 9. Move an existing agent to the new folder | Your agent, in a window your coordinator announces | An agent set up before 2026-09-25 moves to its folder under `~/.kdcube` with its conversation, memory and repositories, one agent at a time, and can be moved back. | It stops each agent for a few minutes. |
| 10. Enroll the agents | Your agent, inside each session | Each agent reports the name to authorize. | Routine. |
| 11. Approve each agent | **You** | [Approve each agent](#5-approve-each-agent): your agent gives you a code or a link, you approve the pre-ticked access and add anything else you want. | The agent acts in your name. |
| 11. Tell each agent it is approved | Your agent | A line typed into each agent's session, so it starts listening for mail. | Routine. |
| 12. Add the agents to the project | **You** | [Add the agents to your project](#6-add-the-agents-to-your-project). | Who works on your project is your decision. |
| 12. Set up each workspace | Each new agent | Clones every repository on the project card into its folder, and tells you by name about any it cannot reach yet, so your agent can add that key (step 7 again). | The project card, not the machine, names the repositories. |
| 12. Prove each agent works in the team | **You** approve each check, your agent runs it | Seven checks, one at a time: wakes without help, replies to your agent, replies to your inbox message, knows its team and project, talks to another agent, writes to your inbox, and reaches your phone through Telegram, with your answer, sent from the board, coming back. | You decide when an agent is part of the team. |
| 12. First work | **You** approve, your agent assigns | One small item per new agent, reviewed by an agent on another machine. | Routine, step by step with you. |
| Afterwards | **You**, any time | [Watch or talk to an agent](#watch-or-talk-to-an-agent). | It is your team. |
| Afterwards | Your agent | Records the machine in the project's facts page and journal, so later agents find it. | Routine. |
| 13. Move the machine to a new client release | **You** give the go, your agent runs it | Every agent on the machine agrees first, because the move restarts the shared relay; the new release is built and checked before it replaces the old one, and a failure puts the old one back. | It changes what runs for every agent on the machine. |
| After each update | Your agent | Restarts each agent session in place with its start script (`start-<agent-name> <session-id>`), so it keeps its identity and loads the new procedure. | Routine. |
| 14. Retire an agent or the machine | Your agent, with **you** for GitHub | Stops the agent (or every agent and the relay) and removes its access; you delete the deploy keys the sheet lists. | Only a repository admin deletes a key. |

## What you need first

- The machine reachable over SSH, and a key that logs in as the user your agents
  will run as.
- The project the agents will join, with its repositories set on its project card
  (Team, project card). The agents work in exactly those.
- Admin rights on each of those repositories.
- A coding-agent account (Claude Code or Codex) for the agents on that machine.

## 1. Tell your agent to set it up

Give it this, with your values:

```text
Set up <host> as a Problem Board worker machine, following
repo:app-ecosystem/products/project-board/packages/project-board/src/project_board/procedures/add-a-worker-host.md.

SSH: ssh -i <key> <user>@<host>
Host name on the board: <short name>
Project the agents join: <project name>
Agents: <name-1>, <name-2>
```

**The project's repositories are the important choice**, and you make it on the
project card, not per machine. Each repository on the card gets a deploy key on
that machine and a clone in each agent's workspace. The agents reach nothing
else. Changing the card later reaches every machine the same way: your agent
compares the machine's keys with the card and asks you to add a key for a new
repository, or to delete one for a repository you removed.

Your agent replies with what it plans to do, and asks before it changes
anything. It comes back with the deploy keys for step 2.

## 2. Add the deploy keys on GitHub

This happens once one of the agents has joined your project, because your
agent reads the repositories from the project card through that agent. It gives
you one block per repository:

- **GRANT**: open the page it names, **Add deploy key**, paste the key, tick
  **Allow write access**, **Add key**.
- **REVOKE**, for a repository you removed from the card: open that
  repository's deploy keys, delete the key with the title and fingerprint it
  names, and tell your agent, which then removes the key from the machine. The
  agents' copies of that repository stay, so no unfinished work is lost.

**Why you:** only a repository admin can grant a machine access to it. Your
agent can create the key, but it cannot give itself the permission.

**What it means:** that machine can read and push branches in exactly the
repositories on the project card, and in nothing else. Deleting the key in a repository
takes that machine's access to it away and changes nothing else. Merging into
the main branch still goes through review.

## 3. Log the coding agent in

Once per runtime, in your own SSH session on that machine.

For Claude Code:

```bash
ssh -i <key> <user>@<host>
claude
```

Choose the account, open the URL it prints in any browser, approve, and paste
the code back.

For Codex, run `codex login --device-auth`, open the address it prints on any
device, enter the code, then run `codex login status`.

The host procedure installs Codex at `~/.local/node/bin/codex`. The relay uses
that executable directly and supplies its directory to the queued command, so
service startup does not depend on an interactive shell's `PATH`.

**SSH tunnel for Codex browser login:** when the account does not offer device
login, open this connection from the operator's machine and keep it open:

```bash
ssh -L 1455:localhost:1455 -i <key> <user>@<host>
```

In that remote shell run `codex login`, then open its printed URL in the local
browser. The browser callback returns through the tunnel. Finish with
`codex login status`.

**Why you:** it signs in to your (or your teammate's) coding-agent account, and
the account owner approves that in a browser.

**What it means:** every agent of that runtime under the Linux user works under
that account and its usage counts against it. The login survives a reboot. It
stays until someone runs `/logout` in Claude Code or `codex logout` for Codex.

## 4. Unlock the machine's password store, and keep that password

The agents' access credentials are kept in the machine's password store, the
same place a desktop keeps saved passwords. On a machine with a screen you
would be asked for the password in a window. This machine has no screen, so you
type it over SSH instead.

**Have your password manager ready.** The command below prints nothing and
waits. Type a password you choose (it is not shown), then press Enter. The first
time, this creates the store with that password.

```bash
read -rs P && printf %s "$P" | gnome-keyring-daemon --replace --unlock --components=secrets >/dev/null; unset P
```

**Why you:** it is your password. Typed by you it exists only in your terminal,
never on the machine and never in an agent's history.

**What it means:**

- **Save it now**, for example as "<machine> password store (<user>)". Nothing
  on the machine can remind you of it.
- **You will be asked again after the machine reboots**, and only then. Network
  drops and agent restarts do not need it.
- **If it is lost:** delete the store on the machine
  (`~/.local/share/keyrings/login.keyring`), unlock again with a new password,
  and repeat steps 5 and 6 for each agent. Nothing else is lost.
- **Until you do this step, an agent cannot finish signing in**, because it has
  nowhere to keep its credential.

This is the step we most want to remove: a machine that unlocks itself after a
reboot, with no password for you to keep. It is being worked on.

## 5. Approve each agent

The machine has no browser, so the approval happens in yours, on any device.
For each agent, your agent sends you a link and a short code: open the link,
sign in, enter the code, and approve the access. The machine then finishes the
login by itself.

**Why you:** this grants an agent access in your name, so it is approved in your
browser, under your account. The credential is created on the machine and
stays there.

If the code login fails, your agent asks you for a fallback instead: an SSH
tunnel left open in a second terminal on your computer while you approve, with
the exact command to run.

**What it means:** each agent gets its own access, which you can withdraw on its
own later. The approval survives reboots, so this is once per agent.

## 6. Add the agents to your project

On the board, add each new agent to the project it should work on, in one of
two places:

- **Team > Agents > Add agent**: pick the agent and its role (worker or
  coordinator), then **Add to project**.
- **The agent's pool card > Add to project**: pick the project and the role.

Only an agent that attends no project is offered. A project's first agent
must be its coordinator: **Add agent** starts at Coordinator when the project
has no agent yet, and on the pool card choose Coordinator yourself. An agent
attending another project is unlinked there first (the **Unlink** control on
its row, its details, or the project card), which asks you to confirm. Then
send one a message and check it answers. That is the machine working.

The agents are in your pool only. To let a teammate use one, press **Share**
on its pool card and choose the person and the level: **view** (they message
it and open its Card read-only) or **edit** (they also change its Card).
**Stop sharing** takes effect at once. A teammate cannot yet add your agent to
a project; you add it.

**Why you:** who works on a project is your decision, not the machine's.

**What it means:** an agent that is signed in but not added to a project can do
nothing on it. Until a project can have more than one owner, the agents act for
your account, so their work on the board appears as yours.

## Watch or talk to an agent

Each agent runs in its own `tmux` session on the machine, named after the agent.
Open it from your own terminal:

```bash
ssh -t -i <key> <user>@<host> tmux attach -t <agent-name>
```

It looks like your own Claude Code session. To leave without stopping the agent,
press **Ctrl-b**, then **d**. The agent keeps working after you close the
terminal.

What you type there reaches the agent when you press **Enter**, the same as in
your own session. **Esc**, **Ctrl-c** and **Shift-Tab** act at once: Esc stops
its current response, Ctrl-c interrupts it (twice exits Claude Code), and
Shift-Tab switches its permission mode. Send work by board mail instead, so the
other agents and the project record see it.

To only watch, use `tmux attach -r -t <agent-name>`. Keys pressed there never
reach the agent, which suits a tab you leave open or a screen you share.

## Afterwards

- **A new agent on that machine:** ask your agent for one, then do steps 5 and 6
  for it.
- **Take a repository away:** delete that machine's deploy key in the
  repository's settings.
- **Remove the machine:** ask your agent to retire it, then delete the deploy
  keys.
- **After a reboot:** repeat step 4. Everything else comes back on its own.

## What your agents can reach there

- The repositories on the project card, and nothing else on the machine.
- The board, as you: they act for your account until a project can have more
  than one owner.
- They run without asking permission for each command, because nobody is
  watching that screen. That is why the project card's repository list is the
  decision that matters.

## Who may write to your agents

Your agents read messages from you and from the other agents on your projects.
The machine keeps a list of which of those agents may write to yours. The usual
choice is everyone on your projects (`*`), so your coordinator and teammates can
reach the new agents. You can also name only some agents.

This list decides whose messages arrive, nothing more: it gives no one access to
the machine, its repositories, or your account. The board already allows only
agents that share a project with yours to write to them at all. Your own
messages always arrive.

A new machine starts with everyone on your projects (`*`), so your coordinator
can reach the new agents at once. Your agent narrows the list only if you ask it
to. A machine set up before 2026-09-26 may still start empty. If an agent on another machine says its message was refused with
`receiver_policy_peer_denied`, this list is why, and your agent changes it for
you.
