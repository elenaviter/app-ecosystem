# service-foundation

Foundation for standalone runnable services.

An application that also ships as its own service needs a second ground
beyond [`app-foundation`](../app-foundation/README.md): something has to
compose the adapters and launch, authenticate a browser session (OIDC)
and a service workload, issue and verify the service's own opaque tokens,
read configuration, answer health and readiness, and invoke the
migrations the application owns. This package is that ground, once, so a
standalone host of an application (the planned standalone Connection Hub
among them) is a thin launcher rather than a project.

Scope boundary, deliberately strict:

- service composition and launcher contracts;
- browser-session (OIDC) and service-workload authentication surfaces;
- opaque-token issuance and verification for the service's own sessions;
- configuration surface, health and readiness;
- migration invocation (the migrations themselves belong to the app).

service-foundation contains no authority models (those are
[`prokura`](../prokura/README.md)), no application behavior, and no
KDCube bundle concepts. It stands on `app-foundation` and is what a
`services/<name>` launcher stands on.

**Version 0.0.1 claims the name.** The modules arrive with the standalone
host work; the running implementation lives inside
[KDCube](https://github.com/kdcube/kdcube) today.

Home: https://github.com/elenaviter/app-ecosystem
