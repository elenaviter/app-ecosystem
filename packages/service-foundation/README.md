# service-foundation

Foundations for standalone runnable services.

## Current Status

`0.0.1` is an installable planning marker that reserves the distribution and
import names. It currently exposes only `service_foundation.__version__`; it is
not yet a service launcher or authentication framework.

```bash
python -m pip install service-foundation
```

## Intended Boundary

A standalone service needs a host layer around its application logic:

- composition and launcher contracts;
- browser-session OIDC and service-workload authentication surfaces;
- opaque-token issuance and verification for service-owned sessions;
- configuration, health, and readiness contracts;
- migration invocation, while the application owns its migrations.

`service-foundation` will own that layer and stand on
[`app-foundation`](../app-foundation/README.md). It will not own application
behavior, Connection Hub authority policy, or KDCube application concepts.

The Connection Hub currently runs as a KDCube application. A future standalone
host can compose the same application over this service layer once the required
host contracts are extracted and verified.

License: MIT. Source: https://github.com/elenaviter/app-ecosystem
