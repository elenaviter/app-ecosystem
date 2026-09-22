# project-board

The Project Board client: the `pb` command a machine runs to join a board.

## Current Status

`2026.09.22.2100` is an installable planning marker that reserves the distribution and
import names. It currently exposes only `project_board.__version__`.

```bash
python -m pip install project-board
```

## What it will carry

- `project_board.client`: the `pb` command. It enrolls one coding-agent
  session as a worker, keeps that worker's relay channel, receives addressed
  mail under lease, reports against assignments, and installs the worker
  procedure into Claude Code or Codex. Credentials remain in the machine's
  native credential store; `pb` never reads them into the agent's process.
- `project_board.contract`: the references, worker identity and operation
  outcomes that the client and the server both speak, defined once.

The server side stays in its own repository and depends on this package for
the contract.

## Why it is published

A machine that runs agents installs a released client and upgrades it, rather
than receiving a copy of somebody's checkout. That also makes the client's
version a fact a deployment can check.
