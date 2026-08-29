# Prokura documentation

Prokura is the delegated-access authority: identity cards for agents and
automations, one central record of authority, per-call resolution at the
service boundary. This folder documents its three forms:

- **`package/`** — the Python package (`prokura` on PyPI): the boundary
  and client SDK, the actor and grant reference format, admission,
  structured denials, claim publication. *(Arrives with the package
  extraction.)*
- **`frontend/`** — the Connection Hub application: the live frontend
  built on top of the package, where a user connects accounts, issues and
  edits identity cards, and watches what the callers did. *(Arrives when
  the application moves into this repository.)*
- **`service/`** — the standalone runnable authority. *(Planned after the
  package.)*

Until each part lands, the running implementation and its documentation
live in [KDCube](https://github.com/kdcube/kdcube).
