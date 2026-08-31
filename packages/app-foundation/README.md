# app-foundation

Host-neutral foundations shared by applications in an ecosystem.

## Current Status

`0.0.1` is an installable planning marker that reserves the distribution and
import names. It currently exposes only `app_foundation.__version__`; the
runtime primitives described below have not yet been extracted into this
package. Do not depend on planned symbols until a release names them as
shipped.

```bash
python -m pip install app-foundation
```

## Intended Boundary

Applications serving real users repeatedly need the same host capabilities:

- principal and service-identity contracts;
- secret-reference resolution and vault adapters;
- Postgres and Redis clients, cache, compare-and-set, and distributed locks;
- HTTP, CSRF, and external-URL utilities;
- events and observability primitives.

`app-foundation` will own those generic capabilities. It will not own delegated
authority models (those belong to [`connection-hub`](https://github.com/elenaviter/app-ecosystem/blob/main/products/connection-hub/packages/connection-hub/README.md)), Connection
Hub behavior, KDCube application concepts, or deployment orchestration.

The production implementations being separated live in
[KDCube](https://github.com/kdcube/kdcube). Releases will move one verified
contract at a time behind compatibility adapters rather than publish a second,
divergent implementation.

License: MIT. Source: https://github.com/elenaviter/app-ecosystem
