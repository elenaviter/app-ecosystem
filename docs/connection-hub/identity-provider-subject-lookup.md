# Identity provider-subject lookup (service-only)

## 2026-10-07: live contract (W609)

`identity_provider_subject_resolve` answers one question for a **registered
service**: "which account of provider P is platform user U linked to?" It
returns the provider subject of that one link, for example a Telegram user id,
and nothing else. Problem Board uses it to post each project member's
notifications into their own Telegram chat.

It is a first-class Hub operation, not a widened form of an existing one:

| Operation | Who may call it | Direction | What it returns |
| --- | --- | --- | --- |
| `identity_resolve` | any caller | provider subject → platform user | the edge and the principal |
| `identity_family_resolve` | the signed-in person, **for themselves** | platform user → their identity family | the caller's own family; any other user is refused (`identity_family_cross_user_access_denied`) |
| **`identity_provider_subject_resolve`** | **a registered admission service, by its own key** | platform user → one provider subject | `{provider, provider_subject}` only |

`identity_family_resolve` stays caller-scoped. Before W609, Problem Board looked
people up through it, so only the person who triggered a notification could be
found.

### Request

`POST …/connection-hub@1-0/operations/identity_provider_subject_resolve`:

```json
{
  "platform_user_id": "cognito:<sub>",
  "provider": "telegram",
  "service_proof": {"service_id": "problem-board", "timestamp": "<unix seconds>",
                    "nonce": "<hex>", "signature": "<hex>"}
}
```

`platform_user_id` is a **typed platform id**, matched exactly against the
edge's target. It is never parsed as an actor id, and no id form is mapped to
another: `cognito:<sub>` and a legacy plain id are different users here.

### Signature

There is no delegated token, so the lookup has its own token-less format,
domain-separated from the delegated-admission proof:

```text
HMAC-SHA256(service secret,
  "connection-hub.identity-provider-subject.v1\n" + service_id + "\n" + timestamp + "\n" + nonce + "\n"
  + "identity_provider_subject_resolve\n" + "urn:kdcube:identity:provider-subject:<provider>\n"
  + sha256(canonical JSON {"platform_user_id", "provider"}))  → lowercase hex
```

A delegated-admission proof never verifies here, and this proof never verifies
there. Both Connection Hub and Problem Board pin the same contract vector in
their tests.

### Who may call it

All of these must hold, and each is tested:

1. **A registered service.** `connections.delegated_credentials.admission` is
   `enabled`, and `services.<service_id>` exists and is enabled.
2. **Purpose binding.** That service's `resources` include
   `urn:kdcube:identity:provider-subject:<provider>`, and this Hub has an
   **enabled** authenticator for that provider.
3. **Its own key.** The proof verifies with **that service row's own**
   `secret_ref`, at least 32 bytes. A secret is never shared with another
   service.
4. **Fresh and once.** The timestamp is within `max_clock_skew_seconds`. The nonce is
   claimed once in Redis (`SET NX`, `nonce_ttl_seconds`), shared by every Hub
   replica; there is no process cache.

The request's user session is **never** read. A person, a delegated or MCP
client Card, or a browser cannot call it, because none of them holds the
service key.

### Answers

| Answer | Meaning |
| --- | --- |
| `{"ok": true, "provider", "provider_subject"}` | exactly one active link |
| `identity_not_linked` | no active link (a revoked edge counts as none) |
| `identity_lookup_ambiguous` | more than one active link for that provider; nothing is returned |
| `identity_lookup_not_permitted` (403) | not a registered service, the resource is not listed, or the provider has no enabled authenticator |
| `identity_provider_subject_resolve_requires_user_and_provider` (400) | `platform_user_id` or `provider` is missing |
| `identity_lookup_requires_service_proof` (403) | no proof, or its `service_id`, `nonce` or `signature` is missing |
| `identity_lookup_proof_invalid` (403), `reason` | `service_secret_unavailable`, `timestamp_invalid`, `timestamp_outside_window` or `signature_invalid` |
| `identity_lookup_proof_replayed` (403) | the nonce was already used |

There is no list, batch or prefix form, so it cannot enumerate users.

### Audit

Every answer logs one line: `[connection-hub.identity_provider_subject_resolve]
service=… platform_user=… provider=… outcome=…`. The provider subject is
**never** logged.

### Registering a service (configuration)

In Connection Hub's descriptor:

```yaml
connections:
  delegated_credentials:
    admission:
      enabled: true
      services:
        problem-board:
          label: "Problem Board notification recipients"
          enabled: true
          secret_ref: "connections.delegated_credentials.admission.services.problem-board.signing_secret"
          resources:
          - "urn:kdcube:identity:provider-subject:telegram"
```

**The secret.** Generate one new random value (`openssl rand -hex 32`), never reusing
another secret. Set the same value in Connection Hub's secret above and in the
calling service's own secret; for Problem Board that is
`connection_hub.identity_lookup_secret`. Never print it. Reload Connection Hub
first, then the service.

### Source map

- `apps/connection-hub@1-0/entrypoint.py`: `_identity_provider_subject_resolve`,
  `identity_subject_lookup_signature`, and the `identity_provider_subject_resolve`
  operation.
- `apps/connection-hub@1-0/tests/test_w609_identity_provider_subject.py`.
- Problem Board's caller: `services/notify.py` `_chat_id` /
  `telegram_subject_lookup_signature`.
