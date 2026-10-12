---
id: app-ecosystem.project-board.procedure.claude-account-profiles
title: Claude Account Profiles
summary: Run each Claude Code agent under its own Claude account folder, and move agents that ran on the host's global login into it, copy first and verify.
tags: [procedure, problem-board, claude code, account, profile, migration]
keywords: [CLAUDE_CONFIG_DIR, claude-accounts, pb worker claude-profile, bind, migrate, rollback, resume, login]
see_also:
  - ./enroll-an-agent.md
  - ./add-a-worker-host.md
---

# Claude Account Profiles

Revision 1 (2026-10-12).

Claude Code keeps one login, one settings file, one set of skills and one
history per configuration folder, the folder `CLAUDE_CONFIG_DIR` names. Without
it every session on a machine shares `~/.claude` and the one login in
`~/.claude.json`, so a machine cannot run agents of two Claude accounts, and
the board cannot tell which account an agent's usage belongs to.

Problem Board gives each account its own folder, named after the account
email: `~/.claude-accounts/<email>`. Every Claude agent runs in the folder of
its account (operator, 2026-10-12: "we re not going to work wit unscoped
claude agents"). Its card reads that folder's login and usage, never the
host's global login, and its Resume line starts it in that folder. A new
Claude session started without its folder does not enroll; an agent already
enrolled from `~/.claude` has no Resume line and no account on its card until
it is moved.

This page is for a person who runs agents on a machine: a new agent, an agent
already running on the global login, and checking the result. Nothing here
starts a model for you, logs in for you, or copies a login between machines.

## The rules

1. **The email decides the folder.** The folder is `~/.claude-accounts/<email>`,
   lower case. Nobody types an account ID.
2. **The login inside the folder is checked.** When an agent enrolls, pb reads
   the account the folder is logged in to. If it is not the account the folder
   is named after, nothing is bound and the agent says so.
3. **A binding is not switched by a restart.** An agent started later in
   another folder keeps its binding; the card shows the difference for the
   person to resolve.
4. **Usage is attributed only when it is verified.** An agent whose folder is
   logged in to another account, or not logged in, shows no account, never
   someone else's.
5. **`env CLAUDE_CONFIG_DIR=… claude`, never `CLAUDE_CONFIG_DIR=… claude`
   through a shell function.** A function or alias named `claude` that picks a
   folder by the current directory would replace the folder. `env` runs the
   real program, and the card's Resume line names the program by its full path.

## A new agent

1. Pick the account. `ACCOUNT=<email>`.
2. Install the worker skill and its settings into that account's folder. This
   creates the folder:

   ```bash
   pb procedure install --target claude-code --claude-config-dir "$HOME/.claude-accounts/$ACCOUNT"
   ```

   It writes the skill under `skills/` and two entries in the folder's
   `settings.json`: the status line command, which Claude runs while the session
   is open to hand pb the live usage, and the Stop hook that keeps the inbox
   watch running. A Claude session reads these from its own folder only.
3. Start the agent in that folder, as the board's **Connect a machine** panel
   shows:

   ```bash
   cd "$HOME/.kdcube/pb/workspaces/$ALIAS" && env CLAUDE_CONFIG_DIR="$HOME/.claude-accounts/$ACCOUNT" claude --add-dir "$HOME/.kdcube" --dangerously-skip-permissions --disallowedTools AskUserQuestion
   ```

4. The first start in a new folder shows Claude's login. Log in with the
   ACCOUNT account. On macOS the login is kept in the Keychain for this folder
   only; on Linux in the folder. Other folders keep their own.
5. Enroll as usual. `pb worker listen` binds the agent and
   `pb worker claude-profile status` shows it `verified`.

The next agent of the same account uses the same ACCOUNT and starts without a
login.

## Agents already running on the global login

Each step says who does it. The person owns which account each agent belongs
to and every login. An agent may prepare and run the commands only after the
person's yes.

1. **See every agent on the machine** (agent or person):

   ```bash
   pb worker claude-profile status
   ```

   For each Claude agent it prints its folder, `profile_state` and, for an
   agent not yet bound, `stored_email`: the account email the board recorded
   for it, read from the machine's global login. That email is a proposal, not
   proof: the global login may have changed since.

2. **Confirm each agent's account** (person). For each agent, say which email
   it belongs to. Then:

   ```bash
   pb worker claude-profile bind --runtime-session-id <session-id> --email <email> --confirm
   ```

   It records the folder `~/.claude-accounts/<email>` for that agent and prints
   its complete Resume line. An email the board has not recorded for the agent
   is accepted only once that folder is logged in to it (log the folder in first
   by starting `env CLAUDE_CONFIG_DIR="$HOME/.claude-accounts/<email>" claude` and
   quitting after the login).

3. **Install the skill and settings into each new folder** (agent or person),
   once per folder:

   ```bash
   pb procedure install --target claude-code --claude-config-dir "$HOME/.claude-accounts/<email>"
   ```

4. **Stop the agent** (person). Its history is copied next, and a running
   agent keeps writing it. End the session (`/exit`, or close its tmux
   window). Agents of the same account can be moved one by one.

5. **Preview, then copy** (agent or person):

   ```bash
   pb worker claude-profile migrate --runtime-session-id <session-id>
   pb worker claude-profile migrate --runtime-session-id <session-id> --apply --session-stopped
   ```

   The first line writes nothing: it lists each file, `copy`, `present` (same
   file already there) or `conflict`, and `blockers`. `--session-stopped` is
   the person's word from step 4 that the agent has exited; without it nothing
   is copied. `session_running` means
   the history was written in the last two minutes; `conflict` means the folder
   already holds a different file of that name. Either stops `--apply` before
   anything is copied.

   `--apply` copies only that agent's own files: its history, its subagent
   files, its file checkpoints and task lists. The project folder's memory is
   shared by every session that ran in that folder, so it comes along only with
   `--with-memory`, after the person confirms it is this agent's. It never
   copies a login, `.claude.json`, settings, plugins, `CLAUDE.md`, agents,
   commands, or another agent's history; the profile's settings hold only what
   step 3 installed. Each copy is checked against the original by hash. The originals
   stay in `~/.claude`: they are the backup. An interrupted `--apply` can be
   run again and copies only what is missing. Neither folder may be reached
   through a symbolic link, and none may sit between a folder and a copied
   file; each copy goes to a new temporary name first and never replaces an
   existing file.

6. **Resume it with the line `--apply` printed, or the card's Resume** (person).
   The line is complete: folder, program path, the same session ID, working
   folder and flags. The first resume in a new folder asks for that account's
   login once (macOS: the Keychain entry is per folder).

7. **Check** (agent, in the resumed session):

   ```bash
   pb worker listen --runtime-kind claude-code --runtime-session-id <session-id>
   pb worker claude-profile status
   ```

   The agent is `verified` and the card says the account is read from this
   session's own profile. The card's Resume dialog shows the new line on both
   tabs, with no part to edit.

8. **Record coverage** (agent). For each machine: every agent with its folder
   and state, and any agent left on the global login and why. A machine counts
   as moved when every Claude agent on it is `verified`.

### Undo

Before the agent resumed in its new folder:

```bash
pb worker claude-profile rollback --runtime-session-id <session-id>
```

removes exactly the files `--apply` created, then the folders it created when
they are empty, and never a folder tree. Once the agent has run in the new
folder its history there differs from the copy, and rollback refuses rather
than lose it. A rolled-back agent is moved again with `migrate --apply`; its
originals are still in `~/.claude`.

## Two machines, two operators

A folder, its login and its registry (`~/.claude-accounts/registry.json`, the
confirmed account-ID-to-folder map) belong to the machine and its user. Each
machine is moved on its own, with its own logins. Never copy a folder, a login
or the registry to another machine, and never move another person's agents.

## What the card shows

| Card | Meaning |
| --- | --- |
| this session's own profile, verified | read from the agent's folder; the login is the bound account |
| profile logged in to another account | the folder's login is not the bound account; usage is not attributed |
| profile not readable / not bound | not moved yet, or no login in its folder; usage is not attributed and there is no Resume line |
