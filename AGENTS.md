# Working in this repository

Operating instructions for coding agents (and a fair summary for humans).

## What this repository is

Components an application ecosystem needs, as installable packages, plus
public application bundles that are their live implementations. First
resident: Prokura, the delegated-access authority (package in
`packages/prokura`, its Connection Hub frontend application in `apps/`, the
standalone service later).

## Layout contract

- `packages/<dist-name>/` — one shipped package per folder: its own
  `pyproject.toml`, `README.md` (the PyPI page), `src/<import_name>/`.
- `apps/<app-name>/` — public application bundles, registered in a KDCube
  deployment by git path.
- `docs/<component>/` — ALL documentation, one folder per component
  (`docs/prokura/package/`, `docs/prokura/frontend/`,
  `docs/prokura/service/`). Docs change in the same pull request as the
  behavior they describe. Docs are public: no secrets, no private paths,
  no links that a reader here cannot open.
- `journal/README.md` — the pointer index into the maintainers' journal
  store. Add a row in the same change as any significant move; titles and
  dates only, the store is not public.

## Rules

- Everything here is public from the first commit. No secrets, tokens, or
  credentials anywhere, including examples and tests. No private
  repository paths.
- The Prokura implementation in this repository is authoritative for the
  authority modules already extracted. Behavior changes land here with their
  package evidence, and KDCube consumes them through its host adapter and
  integration tests. During migration, KDCube compatibility modules re-export
  Prokura; they never fork its implementation.
- Say what things are, plainly. The README positioning (the register
  parallel) is the voice of this repository.
- Commit messages are audited before push: plain, factual, no internal
  codenames.
