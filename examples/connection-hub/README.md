# Connection Hub examples

Connection Hub is the product through which users connect external accounts
and govern what applications, agents, and automations may do on their behalf.
Its current runnable form is the KDCube application with the technical app id
`connection-hub@1-0`.

The [`connection-hub`](../../packages/connection-hub/README.md) Python package
defines the product's card, catalog, admission, signing, and structured-denial
contracts. The KDCube application supplies the user interface and hosted
authority operations.

```text
Connection Hub
├── KDCube application: apps/connection-hub@1-0
├── Python library and client SDK: packages/connection-hub
└── standalone service host: planned as services/connection-hub
```

## Runnable examples

| Example | Integrates with | Uses | Demonstrates | Supporting guide |
| --- | --- | --- | --- | --- |
| [Direct-admission protected service](direct-admission-service/README.md) | Connection Hub's direct-admission operation | `connection-hub` request and workload-signing contracts | An external FastAPI service forwards an opaque delegated bearer with an independently signed workload proof, receives a current allow or denial, and still enforces its own domain rule. | [Protect a service with direct admission](../../docs/connection-hub/recipes/direct-protected-service.md) |

Each runnable example carries its own installation, run, and test instructions.
Example credentials are placeholders; real credentials belong in the
deployment's secret provider.
