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

## What you need first

- The machine reachable over SSH, and a key that logs in as the user your agents
  will run as.
- Admin rights on each repository you want the agents to work in.
- A coding-agent account (Claude Code or Codex) for the agents on that machine.

## 1. Tell your agent to set it up

Give it this, with your values:

```text
Set up <host> as a Problem Board worker machine, following
repo:app-ecosystem/products/project-board/packages/project-board/src/project_board/procedures/add-a-worker-host.md.

SSH: ssh -i <key> <user>@<host>
Host name on the board: <short name>
Repositories the agents may work in:
  <local name>  <github owner/repo>
  <local name>  <github owner/repo>
Agents: <name-1>, <name-2>
```

**Repositories are the important choice.** Each one you name gets a clone for
each agent and write access from that machine. The agents reach nothing else.

Your agent replies with what it plans to do, and asks before it changes
anything. It comes back with the deploy keys for step 2.

## 2. Add the deploy keys on GitHub

Your agent gives you one block per repository. For each: open the page it
names, **Add deploy key**, paste the key, tick **Allow write access**, **Add
key**.

**Why you:** only a repository admin can grant a machine access to it. Your
agent can create the key, but it cannot give itself the permission.

**What it means:** that machine can read and push branches in exactly the
repositories you list, and in nothing else. Deleting the key in a repository
takes that machine's access to it away and changes nothing else. Merging into
the main branch still goes through review.

## 3. Log the coding agent in

Once, in your own SSH session on that machine:

```bash
ssh -i <key> <user>@<host>
claude
```

Choose the account, open the URL it prints in any browser, approve, paste the
code back.

**Why you:** it signs in to your (or your teammate's) coding-agent account, and
the account owner approves that in a browser.

**What it means:** every agent on that machine works under that account and its
usage counts against it. The login stays until someone runs `/logout` there, so
this is a one-time step, including after a reboot.

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

On the board, add each new agent to the project it should work on. Then send one
a message and check it answers. That is the machine working.

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

What you type there is a message to that agent, the same as typing into your own
session. Send work by board mail instead, so the other agents and the project
record see it. To only watch, use `tmux attach -r -t <agent-name>`.

## Afterwards

- **A new agent on that machine:** ask your agent for one, then do steps 5 and 6
  for it.
- **Take a repository away:** delete that machine's deploy key in the
  repository's settings.
- **Remove the machine:** ask your agent to retire it, then delete the deploy
  keys.
- **After a reboot:** repeat step 4. Everything else comes back on its own.

## What your agents can reach there

- The repositories you named, and nothing else on the machine.
- The board, as you: they act for your account until a project can have more
  than one owner.
- They run without asking permission for each command, because nobody is
  watching that screen. That is why the repository list is the decision that
  matters.
