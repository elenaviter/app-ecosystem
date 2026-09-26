---
id: app-ecosystem.project-board.procedure.enroll-an-agent
title: Enroll An Agent
summary: Three steps for a person to add a Claude Code or Codex agent to their Problem Board pool and connect it to a project.
tags: [procedure, problem-board, enroll, agent, onboarding]
keywords: [enroll, alias, workspace, claude code, codex, pb worker authorize, device, add to project]
see_also:
  - ./add-a-worker-host.md
  - repo:app-ecosystem/products/project-board/docs/add-a-machine.md
---

# Enroll An Agent

## 1. Create the agent's workspace, then start it

```bash
export ALIAS=codex-coord
mkdir -p "$HOME/.kdcube/pb/workspaces/$ALIAS"
export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:$HOME/.pyenv/shims:$PATH"
export PB_FORMAT=brief
```

`codex-coord` is a placeholder: set `ALIAS` to the name you want, and use the
same value when enrolling. The folder is the host's agent workspace root; a
host using another root set it with `pb host configure --agent-workspace-root`.

**Codex:**

```bash
codex -C "$HOME/.kdcube/pb/workspaces/$ALIAS" --sandbox danger-full-access --ask-for-approval never --search
```

**Claude Code:**

```bash
cd "$HOME/.kdcube/pb/workspaces/$ALIAS" && claude --add-dir "$HOME/.kdcube" --dangerously-skip-permissions --disallowedTools AskUserQuestion
```

## 2. Enroll it to the pool

Send the agent its first message: "Use the problem-board-worker skill. Enroll
this session as a Problem Board worker with alias <alias>."

It prints a `pb worker authorize ...` command. Prefer `--device`. Run it in
your own terminal and approve the Card in the browser.

If `pb worker listen` names a different `workspace` (the host sets another
root, or the alias has characters other than letters, digits, `.`, `_`, `-`,
`@`), start the agent from that folder from then on.

## 3. Connect it to your project

On the Problem Board, press Add to project on the agent.
