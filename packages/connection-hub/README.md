# Connection Hub

The Python library and client SDK for the Connection Hub product.

Connection Hub gives every agent, automation, and connected application its own
delegated-access card. The card records whose authority the caller uses, which
resources and operations it may reach, which connected accounts it may use,
and when that authority expires. A service resolves the current card and
current capability catalog at the operation boundary, so an edit or revocation
applies to the next call.

## Install

```bash
python -m pip install connection-hub
```

`0.0.3` is an alpha release. It contains the portable implementation used by
the Connection Hub application in this repository:

- versioned delegated cards and capability catalogs;
- live card/catalog admission for managed REST, MCP, and named-service calls;
- delegated OAuth client and connected-account policy contracts;
- structured, actionable denial results;
- direct protected-service admission with an opaque delegated bearer and an
  independent replay-protected workload proof;
- explicit host ports for storage, identity, dispatch, secrets, and live
  delivery.

The same product is currently hosted in KDCube under the technical app id
[`connection-hub@1-0`](../../apps/connection-hub@1-0/README.md). The library
owns portable authority semantics and client contracts; the application host
supplies authenticated sessions, storage, secret resolution, Redis protocol
state, HTTP surfaces, and the user interface.

## Direct Protected-Service Admission

An external backend can accept a user's opaque delegated bearer and ask the
Connection Hub for a live decision about one concrete operation. The backend
also signs the request with its own registered workload secret; possession of
the user bearer alone is not service identity.

```python
import secrets
import time

from connection_hub.delegated_credentials.admission import (
    AdmissionRequest,
    sign_admission_request,
)

request = AdmissionRequest(
    resource="https://api.example.test/customers",
    operation="customers.search",
)
timestamp = str(int(time.time()))
nonce = secrets.token_urlsafe(24)
signature = sign_admission_request(
    secret=service_signing_secret,
    service_id="crm-api",
    timestamp=timestamp,
    nonce=nonce,
    delegated_token=user_delegated_bearer,
    request=request,
)
```

The service sends the semantic request body and the four `X-Connection-Hub-*` proof
headers to the configured Connection Hub admission endpoint, with the opaque
delegated bearer in `Authorization: Bearer ...`. An allow response contains a
service-scoped subject and only the bounded authority relevant to that
operation. It never returns provider credentials or the platform's internal
user id.

See the runnable
[`direct-admission-service`](../../examples/connection-hub/direct-admission-service/README.md)
and the [deployment recipe](../../docs/connection-hub/recipes/direct-protected-service.md).

## Integration Boundaries

- The service registry authenticates workloads and binds each service to
  resource selectors. It does not duplicate the operation/grant catalog.
- Every decision intersects the delegated bearer with the current card and
  active catalog. A cached allow is not an authority source.
- Connected-account credential resolution is a separate trusted operation;
  direct admission does not export provider secrets.
- A protected backend still applies its own domain authorization after
  Connection Hub admission.

## Documentation

- [Connection Hub architecture and semantic requirements](../../docs/connection-hub/connection-hub-architecture.md)
- [Delegated authority and admission](../../docs/connection-hub/package/delegated-authority-and-admission.md)
- [Delegated access cards](../../docs/connection-hub/package/delegated-cards.md)
- [OAuth delegated credential protocol](../../docs/connection-hub/package/oauth-delegated-credential-protocol.md)
- [Package extraction boundary](../../docs/connection-hub/package/extraction-architecture.md)
- [Package release procedure](../../docs/package-releases.md)

License: MIT. Source: https://github.com/elenaviter/app-ecosystem
