# economics-foundation

Host-neutral accounting and economics contracts for attributable service
usage, pricing, budget admission, reservation, and settlement.

## Current Status

`2026.09.02.1559` is an installable planning marker that reserves the
distribution and import names. It currently exposes only
`economics_foundation.__version__`. The production accounting and economics
implementation has not yet been extracted into this package.

```bash
python -m pip install economics-foundation
```

Do not depend on planned symbols until a later release names them as shipped.
The extraction and first governed-service integration are tracked in
[App Ecosystem issue #2](https://github.com/elenaviter/app-ecosystem/issues/2).

## Intended Boundary

The package will own portable contracts and logic for:

- service usage and accounting records;
- caller, owner, service, operation, and execution attribution;
- pricing inputs and measured or estimated cost;
- budget admission, reservation, release, and settlement;
- idempotent usage recording and settlement;
- host-supplied storage, lock, clock, pricing, and event ports;
- conformance tests shared by applications and runtimes.

`economics-foundation` will not own delegated authority or provider
credentials. Those belong to
[`connection-hub`](https://github.com/elenaviter/app-ecosystem/blob/main/products/connection-hub/packages/connection-hub/README.md).
It will not own KDCube descriptors, application or conversation classes,
Redis/Postgres/S3 clients, Stripe integration, or a user interface. Hosts bind
those facilities to the portable contracts.

The production implementation being transitioned lives in
[KDCube](https://github.com/kdcube/kdcube), under its accounting and economics
modules. Extraction will preserve KDCube behavior through compatibility
imports while moving one characterized contract at a time. Connection Hub
will use the same foundation to attribute governed proxy calls and direct
protected-service usage without importing the KDCube runtime.

License: MIT. Source: https://github.com/elenaviter/app-ecosystem
