# Working on Connection Hub

This directory is the Connection Hub product boundary. Read the repository
root `AGENTS.md` first.

## Ownership

- `apps/connection-hub@1-0` owns the KDCube host application, UI, host
  composition, public app interface, and app-level integration tests.
- `packages/connection-hub` owns host-neutral Python contracts and their
  package tests.
- `packages/connection-hub-cli` owns KDCube application-target composition,
  local caller profiles, operating-system credential custody, the stdio MCP
  bridge, and external-client adapters.
- `../../docs/connection-hub` owns public product documentation.
- `../../examples/connection-hub` owns runnable integrations.

Changes to a portable contract land in the package first. The application
imports or adapts that contract without maintaining a second implementation.
Update the app, docs, examples, and release evidence in the same change when
the contract crosses those boundaries.

## Read First

- [Connection Hub documentation](../../docs/connection-hub/README.md)
- [Run Connection Hub locally with KDCube](../../docs/connection-hub/quick-start-local.md)
- [Connection Hub architecture](../../docs/connection-hub/connection-hub-architecture.md)

## Verification

- Run the complete package test directory from
  `packages/connection-hub/tests`.
- Run the local client helper tests from
  `packages/connection-hub-cli/tests` when that package changes.
- Run the app backend tests from `apps/connection-hub@1-0/tests`.
- Run the KDCube shared bundle suite with the app directory as its explicit
  bundle fixture when app behavior or bundle structure changes.
- Run the widget's declared source checks for UI changes. Build generated UI
  output only through the KDCube bundle build pipeline.

## Releases

The product `release.yaml` records the current Connection Hub release, its
changes, and its component map. Its `product.ref`, package component version,
and `config.version` match the Python distribution version in
`packages/connection-hub/pyproject.toml` and `connection_hub.__version__`.
The KDCube app keeps its own bundle `release.yaml`. Follow
`../../docs/releases.md` for the commit, tag, publication, and verification
order.
