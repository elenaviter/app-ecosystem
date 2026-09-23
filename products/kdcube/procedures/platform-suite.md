---
id: kdcube.procedures.platform-suite
title: "Run The Platform And Connection Hub Test Suites From Source"
summary: "Which interpreter runs a KDCube or Connection Hub suite, which source overlays it needs on the path, which disposable databases the real-backend tests want, and how to read a collection failure that is not a regression."
status: current
tags: [procedure, kdcube, connection-hub, testing, pytest, maintainer]
keywords: [platform suite, chat-processor interpreter, PYTHONPATH overlay, KDCUBE_TEST_REDIS_URL, CONNECTION_HUB_TEST_POSTGRES_DSN, PB_TEST_POSTGRES_DSN, pgvector, dependency preflight]
see_also:
  - ./maintainer-rebuild.md
  - ../../project-board/packages/project-board/src/project_board/procedures/testing.md
---

# Run The Platform And Connection Hub Test Suites From Source

A suite in this repository or in KDCube imports the platform and the products
around it. Run it with one named interpreter and the source overlays it needs,
so a `ModuleNotFoundError` inside an unrelated module is read as what it is, a
missing overlay, and not as a regression.

## One interpreter

The KDCube runtime keeps its dependency set in the chat-processor environment
of the platform checkout, `app/venvs/ai-app/chat-processor/bin/python3.11`
under the checkout root. Use it for KDCube suites and for the Problem Board
application suite. The `pb` client has its own interpreter, the one `pb
source status` names, with the client family's dependencies. Use it for the
`project-board` package suite. A bare system Python lacks the third-party
dependencies of both and fails before reaching any code under test.

The installed distributions in an environment can lag the checkouts. On
2026-09-23 the chat-processor environment held a `connection_hub` from before
the durable authority work, and every KDCube auth suite failed to collect on
`connection_hub.delegated_credentials.authority_config`. The overlays below put
the checkouts ahead of what is installed.

## The overlays

Name the checkouts as variables, keep every path explicit:

```bash
KD=<kdcube checkout>/app/ai-app/src/kdcube-ai-app
AE=<app-ecosystem checkout>

export PYTHONPATH="\
$KD:\
$KD/kdcube_cli/src:\
$AE/packages/app-foundation/src:\
$AE/packages/service-foundation/src:\
$AE/products/connection-hub/packages/connection-hub/src:\
$AE/products/connection-hub/packages/connection-hub-cli/src:\
$AE/products/project-board/packages/project-board/src"
```

Run a KDCube suite from `$KD` with the chat-processor interpreter, and a
Connection Hub suite from
`$AE/products/connection-hub/packages/connection-hub`. Before the run, the
dependency preflight names the first declared distribution the interpreter
lacks, in one sentence:

```bash
<interpreter> "$AE/products/project-board/packages/project-board/src/project_board/procedures/dependency_preflight.py" \
  "$KD/pyproject.toml" "$KD/kdcube_cli/pyproject.toml" \
  "$AE/packages/app-foundation/pyproject.toml" \
  "$AE/packages/service-foundation/pyproject.toml" \
  "$AE/products/connection-hub/packages/connection-hub/pyproject.toml" \
  "$AE/products/connection-hub/packages/connection-hub-cli/pyproject.toml"
```

## Real backends

Tests that need a database skip without their variable and say so in the skip
reason. Give them disposable containers, never a runtime's database:

| Suite | Variable | Backend |
| --- | --- | --- |
| Connection Hub package | `CONNECTION_HUB_TEST_POSTGRES_DSN`, `REDIS_URL` | PostgreSQL, Redis |
| KDCube auth, session projections, authority cutover | `KDCUBE_TEST_REDIS_URL`, `KDCUBE_TEST_POSTGRES_DSN` | Redis, PostgreSQL |
| Problem Board application store | `PB_TEST_POSTGRES_DSN` | PostgreSQL with the `vector` extension (`pgvector/pgvector` image), the store schema creates it |

```bash
docker run -d --name suite-postgres -e POSTGRES_PASSWORD="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)" -e POSTGRES_DB=suite -p 127.0.0.1:55432:5432 pgvector/pgvector:pg16
docker run -d --name suite-redis -p 127.0.0.1:56379:6379 redis:7
```

Read the password back from the container's environment into the variable
(`docker inspect suite-postgres --format '{{range .Config.Env}}{{println .}}{{end}}'`)
and never print it or paste it into a message. A Connection Hub suite reads
`REDIS_URL`, not a test-prefixed name.

## Reading a failure

- A `ModuleNotFoundError` under `connection_hub`, `app_foundation` or
  `kdcube_cli` while collecting: an overlay is missing or behind, not a
  regression. Compare the overlay commit with the checkout the code expects.
- A collection error on a fixture signature (an `__init__() missing` argument
  in a test harness): the test is stale against its own repository. Check the
  same test at the integration branch before attributing it to a change.
- Skips with a database reason: the variable is unset. Provide the backend or
  read the run as the offline subset, never as green coverage of the
  real-backend rows.

The Problem Board application suite, with its own seven overlays and the exact
application path, is in
[the package testing procedure](../../project-board/packages/project-board/src/project_board/procedures/testing.md).
