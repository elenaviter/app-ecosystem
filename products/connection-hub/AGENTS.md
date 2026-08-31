# Working on Connection Hub

This directory is the Connection Hub product boundary. Read the repository
root `AGENTS.md` first.

## Ownership

- `apps/connection-hub@1-0` owns the KDCube host application, UI, host
  composition, public app interface, and app-level integration tests.
- `packages/connection-hub` owns host-neutral Python contracts and their
  package tests.
- `../../docs/connection-hub` owns public product documentation.
- `../../examples/connection-hub` owns runnable integrations.

Changes to a portable contract land in the package first. The application
imports or adapts that contract without maintaining a second implementation.
Update the app, docs, examples, and release evidence in the same change when
the contract crosses those boundaries.

## Verification

- Run the complete package test directory from
  `packages/connection-hub/tests`.
- Run the app backend tests from `apps/connection-hub@1-0/tests`.
- Run the KDCube shared bundle suite with the app directory as its explicit
  bundle fixture when app behavior or bundle structure changes.
- Run the widget's declared source checks for UI changes. Build generated UI
  output only through the KDCube bundle build pipeline.

## Releases

`release.yaml` maps product components. It is not a version source. The Python
distribution version remains in its `pyproject.toml`; the KDCube app keeps its
own bundle `release.yaml`.
