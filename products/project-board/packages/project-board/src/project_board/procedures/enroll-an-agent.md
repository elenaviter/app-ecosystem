---
id: app-ecosystem.project-board.procedure.enroll-an-agent
title: Enroll An Agent
summary: Three steps for a person to add a Claude Code or Codex agent to their Project Board pool and connect it to a project.
tags: [procedure, problem-board, enroll, agent, onboarding]
keywords: [enroll, alias, workspace, claude code, codex, pb worker authorize, device, add to project]
see_also:
  - ./add-a-worker-host.md
  - repo:app-ecosystem/products/project-board/docs/add-a-machine.md
---

# Enroll An Agent

## 1. Create the agent's workspace, then start it

```bash
export ALIAS=my-agent
export ACCOUNT=you@example.com
mkdir -p "$HOME/.kdcube/pb/workspaces/$ALIAS" "$HOME/.claude-accounts/$ACCOUNT"
export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:$HOME/.pyenv/shims:$PATH"
export PB_FORMAT=brief
```

`my-agent` is a placeholder: set `ALIAS` to the name you want, and use the
same value when enrolling. The folder is the host's agent workspace root; a
host using another root set it with `pb host configure --agent-workspace-root`.

For Claude Code, `ACCOUNT` is the email of the Claude account the agent works
under, and `~/.claude-accounts/<email>` is that account's own Claude folder:
its login, settings, skill and history. Install the worker skill into it before
the first start with `pb procedure install --target claude-code
--claude-config-dir "$HOME/.claude-accounts/$ACCOUNT"`. The first start in a
new folder asks for that account's login once.

**Codex:**

```bash
codex -C "$HOME/.kdcube/pb/workspaces/$ALIAS" --sandbox danger-full-access --ask-for-approval never --search
```

**Claude Code:**

```bash
cd "$HOME/.kdcube/pb/workspaces/$ALIAS" && env CLAUDE_CONFIG_DIR="$HOME/.claude-accounts/$ACCOUNT" claude --add-dir "$HOME/.kdcube" --dangerously-skip-permissions --disallowedTools AskUserQuestion
```

## 2. Enroll it to the pool

Send the agent its first message: "Use the problem-board-worker skill. Enroll
this session as a Project Board worker with alias <alias>."

It prints a `pb worker authorize <profile> --device` command. Run it in your
own terminal: it prints a link and a short code. Open the link on your own
device, signed in to your own account, enter the code, and approve the Card.

If `pb worker listen` names a different `workspace` (the host sets another
root, or the alias has characters other than letters, digits, `.`, `_`, `-`,
`@`), start a fresh session in that folder; an old session resumes only from
the folder it was started in.

## 3. Connect it to your project

On the Project Board, press Add to project on the agent.
