---
id: kdcube.procedures.maintainer-rebuild
title: "Rebuild A KDCube Runtime As A Maintainer"
summary: "How a change reaches a running KDCube runtime, which refresh form each kind of change needs, and how an actively developed distribution the images import is connected from source in the same build."
status: current
tags: [procedure, kdcube, maintainer, refresh, rebuild, runtime]
keywords: [kdcube refresh, --path, --build, maintainer-local-python-package, actively developed distribution, source selection, verification in the container]
---

# Rebuild A KDCube Runtime As A Maintainer

A KDCube runtime runs the platform source that was staged into its work
directory, inside containers. A bare `kdcube refresh` never restages code: it
rebuilds the copy already staged, so a fix verified in a checkout and "not
working" after a refresh has usually never reached the runtime. Name the
action by the tree the change is in.

## Did my change reach the running system?

| You changed | It reaches the runtime when | Not enough |
| --- | --- | --- |
| Platform or SDK Python (`kdcube_ai_app/...`), server-rendered pages, built-in bundle code | `kdcube refresh --path "$REPO" --build`, which restages the work directory from the checkout, rebuilds the images and restarts | bare `refresh --build` (rebuilds the old staged copy), restarting containers |
| A distribution the images import that a maintainer develops locally (an app-ecosystem package, or any other) | the same refresh, with every such distribution staged in the SAME build through `--maintainer-local-python-package DIST=SOURCE_DIR` | rebuilding without the selector, which installs the published version and makes the symptom look like the platform change failing |
| Widget `src/` | the same refresh, or a bundle reload for the widget's bundle, the pipeline builds `dist/` | editing `src/` alone, building widgets by hand |
| Descriptor content (`bundles.yaml`, secrets) | `bundle config apply --descriptors-location <dir>` (with `--reload`), or `bundle reload <bundle-id>` | `refresh`, which preserves the work directory's `config/` |
| Bundle props only | picked up per request or on reload, no restart | nothing further |

## Actively developed distributions from source

Select each distribution by the directory holding its `pyproject.toml`, never
by a `src` path, and select what that `pyproject.toml` declares when those
checkouts are ahead of the published releases. Repeat the flag per
distribution. The selection survives a dependency moving out of the platform
requirements into its application's own layer: the build plan installs every
selected source whether or not the platform files name it.

```bash
kdcube refresh --tenant <t> --project <p> --path "$REPO" --build \
  --maintainer-local-python-package "app-foundation=$APP_ECOSYSTEM_REPO/packages/app-foundation" \
  --maintainer-local-python-package "connection-hub=$APP_ECOSYSTEM_REPO/products/connection-hub/packages/connection-hub" \
  --maintainer-local-python-package "<distribution>=$APP_ECOSYSTEM_REPO/<path to its package directory>"
```

`--maintainer-local-python-package` requires `--build`: the override is
installed while the images are constructed. The CLI copies each selected
source into the Docker build context, every Python-bearing image installs the
ordinary requirements without the selected distributions, then the selected
sources with the extras those requirements asked for, then the exact sources
again without dependencies, so what runs is what was selected. The CLI removes
the transient build-context copy after the build. A process that runs outside
the containers (a relay, a CLI the operator starts on the machine) has its own
virtual environment: install the distributions it imports there, the flag
changes image contents only.

## Rules

- Before declaring a code fix "not working", confirm the runtime runs it:
  which source selector the last refresh used (`--path`, `--upstream`,
  `--latest`, `--release`). Bare `refresh` is old code.
- When asking the operator to refresh for a platform-code change, say
  `refresh --path "$REPO" --build` and name every maintainer source the build
  needs. Exit 0 plus "every container started" is not verification.
- Verify in the container, not in the checkout: ask the running process for a
  symbol the change introduced, and probe the endpoint unauthenticated (an MCP
  initialize answers 401 with its challenge when the stack is healthy).
- The rebuild is the operator's runtime action. Ask with the exact command and
  wait, do not run it unprompted.
- After a refresh, confirm the containers are up (`docker compose ps` in the
  runtime's docker directory) whatever the refresh printed. If they are not,
  run `kdcube start --tenant <t> --project <p> --path "$REPO"` and report the
  refresh output: a refresh stops the stack first, and a CLI without the fix
  for a failed image receipt (kdcube#304) ended there with the stack down.

Detail for the local operator flow, with the environment variables spelled
out: the CLI cheat sheet in the deployment repository that hosts your
descriptors, section Maintainer Local Python Package Source.
