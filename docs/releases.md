# Releasing App Ecosystem products and foundations

Every releasable piece has an explicit source location, version source, release
record, verification gate, and publication path. A release always identifies a
committed repository snapshot. A version bump in a working tree is not a
release.

## Release units

| Piece | Source | Version source | Release record | Publication |
| --- | --- | --- | --- | --- |
| Connection Hub product | `products/connection-hub` | `products/connection-hub/release.yaml` | `products/connection-hub/release.yaml` | Product tag and component workflows |
| `connection-hub` Python distribution | `products/connection-hub/packages/connection-hub` | `pyproject.toml` and `connection_hub.__version__` | Product `release.yaml` | `publish-python-package.yml` |
| `connection-hub-cli` Python distribution | `products/connection-hub/packages/connection-hub-cli` | `pyproject.toml` and `connection_hub_cli.__version__` | Product `release.yaml` | `publish-python-package.yml` |
| Connection Hub macOS presence helper | `products/connection-hub/packages/connection-hub-presence-helper-macos` | Product `release.yaml`; injected into the binary and app metadata by `build-app.sh --version` | Product `release.yaml` | Signed and notarized zip plus manifest on the matching GitHub release |
| Connection Hub KDCube app | `products/connection-hub/apps/connection-hub@1-0` | Bundle release record | `products/connection-hub/apps/connection-hub@1-0/release.yaml` | KDCube bundle source ref |
| `app-foundation` | `packages/app-foundation` | `pyproject.toml` and `app_foundation.__version__` | `packages/app-foundation/release.yaml` when released | `publish-python-package.yml` |
| `service-foundation` | `packages/service-foundation` | `pyproject.toml` and `service_foundation.__version__` | `packages/service-foundation/release.yaml` when released | `publish-python-package.yml` |
| `harness-foundation` | `packages/harness-foundation` | `pyproject.toml` and `harness_foundation.__version__` | `packages/harness-foundation/release.yaml` when released | `publish-python-package.yml` |
| `capabilities-foundation` | `packages/capabilities-foundation` | `pyproject.toml` and `capabilities_foundation.__version__` | `packages/capabilities-foundation/release.yaml` | `publish-python-package.yml` |
| `economics-foundation` | `packages/economics-foundation` | `pyproject.toml` and `economics_foundation.__version__` | `packages/economics-foundation/release.yaml` | `publish-python-package.yml` |

## Version and tag contract

Author release versions as `YYYY.MM.DD.HHMM` in Berlin time, for example
`2026.09.01.1120`. This is a PEP 440 release version. Python package indexes and
installers may display its canonical form without leading zeroes, such as
`2026.9.1.1120`; both forms identify the same Python version.

The repository tag is the exact authored version. A product release record
contains:

- the exact version in `product.ref` and `config.version`;
- a description of the changes in that source snapshot;
- the released component paths and each published component version.

Shared foundations use the same fields in their package-local `release.yaml`
when they are released. One tag may cover several pieces only when the same
committed snapshot intentionally releases them together.

## Release gate

For each piece selected for release:

1. Update its release record with the exact version and changes.
2. Update each published package's `pyproject.toml`, import `__version__`, and
   README to the same authored version.
3. Run the piece's complete tests. `connection-hub` runs
   `products/connection-hub/packages/connection-hub/tests`;
   `connection-hub-cli` runs
   `products/connection-hub/packages/connection-hub-cli/tests`;
   planning-marker foundations run install, import, and version smoke checks.
4. Build the wheel and source distribution with `python -m build`.
5. Run `python -m twine check` on every artifact.
6. Install the wheel in a fresh virtual environment and verify the imported
   version.
7. Commit the complete release snapshot and push it.
8. Create the exact version tag on that commit and push the tag.
9. Dispatch `.github/workflows/publish-python-package.yml` from that tag with
   `package=<distribution>` and `expected_version=<authored version>`.
10. Wait for the workflow to complete, verify the version through the PyPI
    project API, and install it in a second clean environment.

The workflow repeats tests, build, metadata checks, and clean-wheel smoke. For
Connection Hub it also verifies that the product release record, distribution
metadata, import version, workflow input, and source tag all agree.

## Native macOS helper gate

The Connection Hub macOS presence helper is released with the Connection Hub
product version. Before tagging that product release:

1. Add the helper path and exact version to the product `release.yaml` component
   map.
2. Run locked Rust tests, strict Clippy, formatting, release checking, the
   dependency advisory scan, and the packaging lifecycle suite.
3. Build with `scripts/build-app.sh` using the Developer ID Application
   identity, exact 10-character Team ID, matching provisioning profile, and a
   `notarytool` keychain profile.
4. Complete the real provisioned interactive test while a tester is present.
   Cancellation must dispatch no operation; approval must dispatch one exact
   operation; changed input must require another prompt; cleanup must remove the
   disposable protected item.
5. Verify the candidate's signature, entitlement, embedded profile,
   notarization ticket, Gatekeeper assessment, version, architecture, archive
   checksum, and absence of the interactive-check binary and disposable test
   values.
6. Install, upgrade, and uninstall from clean macOS accounts. A failed candidate
   must preserve the current version, and failed protected-session cleanup must
   preserve the installed helper.
7. Attach `ConnectionHubPresenceHelper-<version>.zip` and
   `manifest-<version>.txt` to the GitHub release for the exact product tag.
   Publish the expected Team ID and archive SHA-256 in the release notes.
8. Verify the documented `connection-hub` integration against the released
   artifact before announcing the user-presence path as supported.

The canonical user and maintainer procedure is
[Protect KDCube Management On macOS With User Presence](connection-hub/macos-user-presence-helper.md).

The repository currently has no workflow that publishes this native artifact.
`publish-python-package.yml` publishes Python distributions only. Add a
reviewed native release workflow or execute and record an explicit supervised
GitHub-release attachment procedure before the first helper release.

PyPI versions are immutable. Correct a release with a new calendar version.
After a package is visible on PyPI, consumers may raise their requirement floor
to the released calendar family.

## PyPI authentication

The workflow uses PyPI trusted publishing through GitHub's OIDC identity. Each
PyPI project needs a one-time trusted-publisher registration for:

```text
owner: elenaviter
repository: app-ecosystem
workflow: publish-python-package.yml
environment: (empty)
```

No PyPI token is stored in this repository. A local manual upload is reserved
for initial recovery and uses a credential supplied outside the repository
without writing it into shell history or logs.
