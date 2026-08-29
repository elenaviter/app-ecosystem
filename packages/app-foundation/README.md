# app-foundation

Host-neutral foundation for applications in an ecosystem.

Applications that serve real users keep re-needing the same ground:
knowing who is calling (a person through a browser session, a service
through workload identity), resolving secret references without holding
secrets in code, talking to Postgres and Redis, caching with distributed
locks, speaking HTTP safely (CSRF, external URL discipline), and emitting
events and observability signals. This package is that ground, once.

Scope boundary, deliberately strict:

- generic `Principal` and service identity contracts;
- secret-reference resolution and vault adapters;
- Postgres and Redis clients, cache, compare-and-set, distributed locks;
- HTTP, CSRF, and external-URL utilities;
- events and observability primitives.

app-foundation contains no authority models (those are
[`prokura`](../prokura/README.md)), no Connection Hub behavior, no
KDCube bundle concepts, and no deployment orchestration. It is the layer
both of those stand on.

**Version 0.0.1 claims the name.** The modules are being extracted from
the production implementation inside
[KDCube](https://github.com/kdcube/kdcube), behind existing host
adapters, as part of the Prokura extraction.

Home: https://github.com/elenaviter/app-ecosystem
