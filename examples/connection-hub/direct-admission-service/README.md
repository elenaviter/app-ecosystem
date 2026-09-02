# Direct-admission protected service

This runnable FastAPI backend shows how a service outside KDCube can use a
KDCube-hosted Connection Hub as its delegated-access authority.

The backend calls the Connection Hub product endpoint. It imports `connection-hub`
as the client SDK that defines and signs the admission request.

```text
user-approved external client
  |  Authorization: Bearer <opaque delegated credential>
  v
reference service
  |  same bearer + fresh signed workload proof
  v
Connection Hub delegated_admission
  |  current card x current catalog x service/resource registration
  v
allow: pairwise user + caller-profile ids + bounded authority
deny: structured current-state reason
```

The service never validates the opaque bearer locally and never receives a
provider credential. It proves its own workload identity independently with an
HMAC over its id, timestamp, nonce, bearer hash, and semantic request. An allow
returns pairwise user and caller-profile ids, bounded authority, and current
invocation policy. The service still applies its own domain rule before
returning data.

## Run

Create a virtual environment and install the example:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
```

Configure the protected service. `CONNECTION_HUB_SERVICE_SECRET` must be the same
secret resolved by the Connection Hub's registered service row, but it must be
delivered through each deployment's secret manager rather than committed.

```bash
export CONNECTION_HUB_ADMISSION_URL='http://localhost:8010/api/integrations/bundles/demo-tenant/demo-project/connection-hub@1-0/public/delegated_admission'
export CONNECTION_HUB_SERVICE_ID='reference-customers-api'
export CONNECTION_HUB_SERVICE_SECRET="$REFERENCE_CUSTOMERS_API_SECRET"
export CONNECTION_HUB_RESOURCE='https://reference.example.test/customers'
.venv/bin/uvicorn reference_service.app:create_app --factory --port 8090
```

An authorized client can then call:

```bash
curl -sS http://localhost:8090/customers/search \
  -H "Authorization: Bearer $DELEGATED_BEARER" \
  -H 'Idempotency-Key: customer-search-0185' \
  -H 'Content-Type: application/json' \
  --data '{"query":"north"}'
```

The delegated bearer is obtained through the Connection Hub's user-consent
flow. It is not the protected service's secret and must not be substituted for
workload authentication.

The example operation is read-only. It passes the `Idempotency-Key` and a
digest of the search request into direct admission to demonstrate one-use and
replay semantics. A state-changing service must also keep its own effect
idempotency ledger under that key because Connection Hub records the admission
decision, not the external service's domain write.

See the complete [deployment and integration
recipe](../../../docs/connection-hub/recipes/direct-protected-service.md).

## Verify

```bash
.venv/bin/python -m pytest
```
