# capabilities-foundation

The capabilities agents and apps use, as plain Python.

## Current Status

`2026.09.01.1230` is an installable planning marker that reserves the
distribution and import names. It currently exposes only
`capabilities_foundation.__version__`; the production implementations have
not yet been extracted into this package.

```bash
python -m pip install capabilities-foundation
```

## Intended Boundary

The package will own capability implementations with classical call
surfaces:

- web search and page fetch, with extraction and filtering;
- document rendering: markdown to pdf, pptx, docx, and html;
- isolated code execution: the two-container sandbox engine (a trusted
  supervisor and an executor with no network and no credentials);
- browser automation.

A capability works from any Python code, with no agent machinery inside.
It depends only downward, on
[`app-foundation`](https://github.com/elenaviter/app-ecosystem/blob/main/packages/app-foundation/README.md)
contracts (storage, config and secret resolution, work paths,
observability), received as arguments rather than imported from a host.

Turning a capability into an agent tool is the harness's job
([`harness-foundation`](https://github.com/elenaviter/app-ecosystem/blob/main/packages/harness-foundation/README.md)):
binding, tool identity, routing, recorded results. Connecting capabilities
to an application is the host's act, one layer up; no package here points
back at a host. Authority models stay in `connection-hub`.

The running implementations remain in
[KDCube](https://github.com/kdcube/kdcube). Extraction proceeds capability
by capability; document rendering is the first lane.

License: MIT. Source: https://github.com/elenaviter/app-ecosystem
