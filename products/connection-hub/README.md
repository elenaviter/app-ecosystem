# Connection Hub

Connection Hub is one product with a hosted application and a reusable Python
distribution.

## Components

- [`apps/connection-hub@1-0`](apps/connection-hub@1-0/README.md) is the KDCube
  application, user interface, provider integration host, and protected
  admission surface.
- [`packages/connection-hub`](packages/connection-hub/README.md) is the
  `connection-hub` distribution imported as `connection_hub`. It owns the
  portable authority, card, catalog, admission, and client contracts.
- [`release.yaml`](release.yaml) maps the independently released components.

The product documentation remains in
[`docs/connection-hub`](../../docs/connection-hub/README.md). Runnable
integrations remain in
[`examples/connection-hub`](../../examples/connection-hub/README.md).

Future Connection Hub service hosts belong under `services/` in this product
directory.
