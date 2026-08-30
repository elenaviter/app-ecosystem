# Working in this repository

Operating instructions for coding agents (and a fair summary for humans).

## What this repository is

Components an application ecosystem needs, as installable packages, plus
public application bundles that are their live implementations. First
product: Connection Hub. Its current KDCube application lives in
`apps/connection-hub@1-0`, its Python library and client SDK live in
`packages/connection-hub`, and its standalone service host comes later.

## Layout contract

- `packages/<dist-name>/` — one shipped package per folder: its own
  `pyproject.toml`, `README.md` (the PyPI page), `src/<import_name>/`.
- `apps/<app-name>/` — public application bundles, registered in a KDCube
  deployment by git path.
- `examples/<product-or-component>/` — runnable integrations grouped by the
  product or component they demonstrate, with a group README that maps each
  example to its applications, packages, services, and docs.
- `docs/<component>/` — ALL documentation, one folder per component
  (`docs/connection-hub/package/`, `docs/connection-hub/frontend/`,
  `docs/connection-hub/service/`). Docs change in the same pull request as the
  behavior they describe. Docs are public: no secrets, no private paths,
  no links that a reader here cannot open.
- `journal/README.md` — the pointer index into the maintainers' journal
  store. Add a row in the same change as any significant move; titles and
  dates only, the store is not public.

## Rules

- Everything here is public from the first commit. No secrets, tokens, or
  credentials anywhere, including examples and tests. No private
  repository paths.
- The `connection_hub` implementation in this repository is authoritative for
  the authority modules already extracted. Behavior changes land here with
  their package evidence, and KDCube consumes them through its host adapter and
  integration tests. During migration, KDCube compatibility modules re-export
  the package; they never fork its implementation.
- Say what things are, plainly. Connection Hub is one product across its
  package, application, protocol, examples, and service hosting.
- Commit messages are audited before push: plain, factual, no internal
  codenames.
