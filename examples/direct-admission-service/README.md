# Direct-admission protected service

This runnable FastAPI backend shows how a service outside KDCube can use a
KDCube-hosted Connection Hub as its delegated-access authority.

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
allow: pairwise subject + bounded authority
deny: structured current-state reason
```

The service never validates the opaque bearer locally and never receives a
provider credential. It proves its own workload identity independently with an
HMAC over its id, timestamp, nonce, bearer hash, and semantic request hash. On
an allow, it still applies its own domain rule before returning data.

## Run

Create a virtual environment and install the example:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
```

Configure the protected service. `PROKURA_SERVICE_SECRET` must be the same
secret resolved by the Connection Hub's registered service row, but it must be
delivered through each deployment's secret manager rather than committed.

```bash
export CONNECTION_HUB_ADMISSION_URL='http://localhost:8010/api/integrations/bundles/demo-tenant/demo-project/connection-hub@1-0/public/delegated_admission'
export PROKURA_SERVICE_ID='reference-customers-api'
export PROKURA_SERVICE_SECRET="$REFERENCE_CUSTOMERS_API_SECRET"
export PROKURA_RESOURCE='https://reference.example.test/customers'
.venv/bin/uvicorn reference_service.app:create_app --factory --port 8090
```

An authorized client can then call:

```bash
curl -sS http://localhost:8090/customers/search \
  -H "Authorization: Bearer $DELEGATED_BEARER" \
  -H 'Content-Type: application/json' \
  --data '{"query":"north"}'
```

The delegated bearer is obtained through the Connection Hub's user-consent
flow. It is not the protected service's secret and must not be substituted for
workload authentication.

See the complete [deployment and integration
recipe](../../docs/prokura/recipes/direct-protected-service.md).

## Verify

```bash
.venv/bin/python -m pytest
```
