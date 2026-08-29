# Prokura

One current authority for delegated access.

Prokura gives every agent, automation, and connected application its own
delegated-access card. The card records whose authority the caller uses, which
resources and operations it may reach, which connected accounts it may use,
and when that authority expires. A service resolves the current card and
current capability catalog at the operation boundary, so an edit or revocation
applies to the next call.

The name comes from the commercial-law institution in which delegated signing
authority lives in a register. A third party checks the register rather than a
letter carried by the delegate. Prokura applies that model to software callers.

## Install

```bash
python -m pip install prokura
```

`0.0.2` is an alpha release. It contains the portable implementation used by
the Connection Hub application in this repository:

- versioned delegated cards and capability catalogs;
- live card/catalog admission for managed REST, MCP, and named-service calls;
- delegated OAuth client and connected-account policy contracts;
- structured, actionable denial results;
- direct protected-service admission with an opaque delegated bearer and an
  independent replay-protected workload proof;
- explicit host ports for storage, identity, dispatch, secrets, and live
  delivery.

The currently runnable product is the KDCube-hosted
[`connection-hub@1-0`](../../apps/connection-hub@1-0/README.md) application.
Prokura owns authority semantics; the host supplies authenticated sessions,
storage, secret resolution, Redis protocol state, and HTTP surfaces.

## Direct Protected-Service Admission

An external backend can accept a user's opaque delegated bearer and ask the
Connection Hub for a live decision about one concrete operation. The backend
also signs the request with its own registered workload secret; possession of
the user bearer alone is not service identity.

```python
import secrets
import time

from prokura.delegated_credentials.admission import (
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

The service sends the semantic request body and the four `X-Prokura-*` proof
headers to the configured Connection Hub admission endpoint, with the opaque
delegated bearer in `Authorization: Bearer ...`. An allow response contains a
service-scoped subject and only the bounded authority relevant to that
operation. It never returns provider credentials or the platform's internal
user id.

See the runnable
[`direct-admission-service`](../../examples/direct-admission-service/README.md)
and the [deployment recipe](../../docs/prokura/recipes/direct-protected-service.md).

## Integration Boundaries

- The service registry authenticates workloads and binds each service to
  resource selectors. It does not duplicate the operation/grant catalog.
- Every decision intersects the delegated bearer with the current card and
  active catalog. A cached allow is not an authority source.
- Connected-account credential resolution is a separate trusted operation;
  direct admission does not export provider secrets.
- A protected backend still applies its own domain authorization after Prokura
  admission.

## Documentation

- [Connection Hub architecture and semantic requirements](../../docs/prokura/connection-hub-architecture.md)
- [Delegated authority and admission](../../docs/prokura/package/delegated-authority-and-admission.md)
- [Delegated access cards](../../docs/prokura/package/delegated-cards.md)
- [OAuth delegated credential protocol](../../docs/prokura/package/oauth-delegated-credential-protocol.md)
- [Package extraction boundary](../../docs/prokura/package/extraction-architecture.md)
- [Package release procedure](../../docs/package-releases.md)

License: MIT. Source: https://github.com/elenaviter/app-ecosystem
