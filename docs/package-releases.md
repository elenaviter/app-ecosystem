# Publishing Python packages

Each distribution under `packages/` versions and publishes independently. A
release is made from a committed source snapshot; a working-tree version bump
is not a release record.

## Packages

| Distribution | Import | Package directory |
| --- | --- | --- |
| `prokura` | `prokura` | `packages/prokura` |
| `app-foundation` | `app_foundation` | `packages/app-foundation` |
| `service-foundation` | `service_foundation` | `packages/service-foundation` |
| `harness-foundation` | `harness_foundation` | `packages/harness-foundation` |

## Release Gate

1. Update the selected package's version in `pyproject.toml` and its import
   package `__version__` in the same commit.
2. Update its README so PyPI describes what that version actually ships.
3. Run package tests. Prokura runs its complete test directory; planning-marker
   packages run install/import/version smoke checks.
4. Build both wheel and source distribution with `python -m build`.
5. Run `twine check` on every artifact.
6. Install the wheel in a fresh virtual environment and verify the import and
   exact version.
7. Push the committed release snapshot.
8. Dispatch `.github/workflows/publish-python-package.yml`, selecting the
   package and supplying the exact committed version as `expected_version`.

The workflow repeats the tests, build, metadata check, and clean-wheel smoke
before uploading. It refuses a dispatch whose expected version differs from
the committed package metadata.

## PyPI Authentication

The workflow uses PyPI trusted publishing through GitHub's OIDC identity. Each
PyPI project needs a one-time trusted-publisher registration for:

```text
owner: elenaviter
repository: app-ecosystem
workflow: publish-python-package.yml
environment: (empty)
```

No PyPI token is stored in this repository or in the workflow. A local manual
upload is reserved for initial recovery or bootstrap and must use a credential
supplied outside the repository without writing it into shell history or logs.

PyPI versions are immutable. If a version already exists, fix or extend the
package under a new version rather than attempting to overwrite it.
