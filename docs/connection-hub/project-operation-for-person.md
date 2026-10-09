# Project operation for a proven person (service-only)

`project_operation_authorize_for_person` decides one project operation for a
person a registered service **proves through their provider edge**. It exists
for W615: Problem Board's Telegram webhook runs under one bound caller, so the
ordinary `project_operation_authorize` (which decides for the request's
signed-in caller) would decide a member's Telegram reply for that caller. The
board then correctly refused the mismatched person, and the member's reply was
not delivered.

## How it differs

| Operation | Decides for | Who may call |
| --- | --- | --- |
| `project_operation_authorize` | the request's signed-in caller | any signed-in person, for themselves |
| `project_operations_authorize` | the request's signed-in caller, for many operations in one exchange (the chain is resolved once) | any signed-in person, for themselves |
| `project_operation_authorize_for_person` | the `person_subject` named in the request | only a registered admission service, with its own proof |

The decision itself is the same per-person evaluation: that person's own live
Control and My Cards, the active catalog and the requested grants. **The
request's caller is never used.**

## Request

```json
{
  "project_ref": "work:project:…", "person_subject": "<platform user id>",
  "provider": "telegram", "provider_subject": "<the sender's Telegram user id>",
  "resource": "<the application resource>", "operation": "control.enqueue",
  "required_grants": ["…"], "request_resource": "", "surface": "application",
  "service_proof": {"service_id": "problem-board", "timestamp": "<unix seconds>",
                    "nonce": "<random>", "signature": "<hex>"}
}
```

## Signature

HMAC-SHA256 with the service's **own** signing secret over, joined by `\n`:
`connection-hub.project-operation-person.v1`, `service_id`, `timestamp`,
`nonce`, `project_operation_authorize_for_person`,
`urn:kdcube:project-operation:<provider>`, and the SHA-256 of the canonical JSON
of every request field above except the proof. The domain is distinct from the
W609 lookup's, so neither proof can stand for the other. Contract vector:
`tests/test_w615_project_operation_for_person.py` (also pinned on the board).

## Who may call it, and when it answers

The service row (`delegated_credentials.admission.services.<id>`) must be
enabled and list the resource `urn:kdcube:project-operation:<provider>`; the
provider must have an enabled authenticator; the proof must be within the clock
window, correctly signed with that row's secret (at least 32 bytes), and used
once (Redis `SET NX`, so across every process). Then the person must hold
**exactly** the sending provider edge (`provider_subject`).

| Answer | Meaning |
| --- | --- |
| the decision, plus `person_subject` and `provider` echoed | the person's own Cards decided |
| `project_operation_for_person_request_invalid` (400) | a required field is missing |
| `project_operation_for_person_requires_service_proof` (403) | no proof, or its `service_id`, `nonce` or `signature` is missing |
| `project_operation_for_person_not_permitted` (403) | the service is unknown, not enabled, lacks the resource, or the provider has no enabled authenticator |
| `project_operation_for_person_proof_invalid` (403) | bad timestamp, outside the window, secret unavailable, or the signature does not match this exact request |
| `project_operation_for_person_proof_replayed` (403) | the proof was already used |
| `project_operation_sender_not_linked` (403) | the person does not hold exactly that provider edge |

Each call logs one line with the service, person, provider, project, operation
and outcome; never the provider subject.

## Registering a service

Add the resource to the service's row, for example Problem Board's:

```yaml
problem-board:
  enabled: true
  secret_ref: "connections.delegated_credentials.admission.services.problem-board.signing_secret"
  resources:
  - "urn:kdcube:identity:provider-subject:telegram"
  - "urn:kdcube:project-operation:telegram"
```

No new secret is needed when the service already has its own (W612).

## Source

`apps/connection-hub@1-0/entrypoint.py`: `_project_operation_authorize_for_person`,
`person_operation_request`, `person_operation_signature`, the operation
`project_operation_authorize_for_person`.
