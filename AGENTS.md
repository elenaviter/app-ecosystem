# Working in this repository

Operating instructions for coding agents (and a fair summary for humans).

## What this repository is

Components an application ecosystem needs, organized by product, plus shared
foundation packages. First product: Connection Hub. Its current KDCube
application and Python library live together under
`products/connection-hub`. Its standalone service host comes later.

For the current runnable local product path, start with
[`docs/connection-hub/quick-start-local.md`](docs/connection-hub/quick-start-local.md).

## Layout contract

- `products/<product>/` — one product ownership boundary. Product-specific
  applications, packages, release metadata, and operating instructions live
  below this directory.
- `products/<product>/packages/<dist-name>/` — a product-owned distribution:
  its own `pyproject.toml`, README (the PyPI page), and
  `src/<import_name>/`.
- `products/<product>/apps/<app-name>/` — a product-owned public application
  bundle, registered in a KDCube deployment by git path.
- `packages/<dist-name>/` — shared cross-product foundations only.
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
- `docs/releases.md` - the release contract for every product and shared
  foundation in this repository.

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
  package, application, protocol, examples, and service hosting. Its code is
  scoped under `products/connection-hub`; its public documentation remains
  under `docs/connection-hub`, and runnable examples remain under
  `examples/connection-hub`.
- Commit messages are audited before push: plain, factual, no internal
  codenames.
- Releases use the authored `YYYY.MM.DD.HHMM` calendar version. A release is a
  committed and pushed snapshot whose release record, package metadata, and
  import version agree. Tag that snapshot with the exact version, push the
  tag, then dispatch publication from the tag. Follow `docs/releases.md`.
