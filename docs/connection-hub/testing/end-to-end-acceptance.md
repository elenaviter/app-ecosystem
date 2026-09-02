---
id: connection-hub/testing/end-to-end-acceptance
title: "Connection Hub And Governed MCP End-To-End Acceptance"
summary: "Human-runnable acceptance procedure for delegated cards, once-or-always invocation policy, user-owned external MCP proxying, direct protected-service admission, connected accounts, live consent, revocation, durability, and external clients such as Claude Code."
status: current
tags: ["testing", "connection-hub", "delegated-access", "mcp", "proxy", "admission", "invocation-policy", "consent", "claude-code"]
keywords: ["Connection Hub acceptance", "delegated card test", "allow once", "allow always", "external MCP proxy", "direct admission", "descriptor drift", "live consent", "operation-only consent", "resource_operations", "named services MCP", "Claude Code OAuth", "revocation test"]
updated_at: 2026-09-02
see_also:
  - ../connection-hub-architecture.md
  - ../package/delegated-authority-and-admission.md
  - ../package/delegated-cards.md
  - ../package/oauth-delegated-credential-protocol.md
  - https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/arch/security-and-trust-model-README.md
  - https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/recipes/connections/delegate-kdcube-service-to-external-client-README.md
---
# Connection Hub And Governed MCP End-To-End Acceptance

This procedure proves the complete user-visible and enforcement path for
Connection Hub delegated access. It is suitable for a human tester working
with an agent, and every step is explicit enough to automate later.

The procedure covers:

- a resident KDCube agent using a managed MCP or native named-service tool;
- managed MCP resources such as Conversation MCP and Productivity MCP;
- the named-services MCP door and its exact inner namespace/operation checks;
- connected-account selection and provider claims;
- demand-driven consent, including an operation-only demand whose `claims`
  list is empty;
- live grant delivery, retry, revocation, card durability, and fail-closed
  behavior;
- an external OAuth client, using Claude Code as the worked client;
- a caller-defined managed REST or MCP surface registered by an application;
- a user-owned external MCP server with no Connection Hub integration, called
  through the Connection Hub proxy;
- a directly integrated external service that asks Connection Hub for a live
  decision before executing its own operation;
- one-use and reusable invocation policies, including idempotent retry.

This is an acceptance test, not the authority specification. The decision
model is owned by
[Delegated Authority And Admission](../package/delegated-authority-and-admission.md).

## Pass Rule

The run passes only when the real operation succeeds or is denied at the
expected boundary. A green UI alone is not sufficient. Capture the structured
tool result and the provider-side effect where one exists.

Every accepted invocation must satisfy five independent facts:

| Fact | Example evidence |
| --- | --- |
| Caller and card | The hosted agent or external bearer resolves to the expected current `access_id`. |
| Managed resource and outer permission | The card covers the MCP/REST resource and required outer tool or KDCube grant. |
| Exact inner operation | A named-service request covers the decoded namespace and operation, such as `slack.object.action.post_message`. |
| Connected account and provider claims | The selected account is the intended Slack workspace and holds the provider claims required by that operation. |
| Invocation policy | The operation is reusable, or the one available invocation is reserved by this invocation id and request digest. |

Granting one fact never grants another. In particular, a provider claim such
as `slack:post` does not select a named-service operation, and selecting
`object.action.post_message` does not grant `slack:post` on an account.

## Test Record

Create a record for the run before changing authority. Do not record bearer
tokens, provider tokens, authorization codes, cookies, or client secrets.

Record:

- deployment URL, tenant, project, and deployed revisions;
- signed-in platform user;
- hosted bundle and agent identity;
- conversation ID;
- each tested card's `access_id`, revision, caller label, expiry, and active
  catalog version;
- connected-account label and provider, without credentials;
- disposable Slack DM or channel and a non-sensitive image;
- timestamps for denial, grant, retry, revocation, and restart;
- screenshots and structured result codes listed in the evidence checklist.

Use separate cards for the hosted agent, Claude Code, and any automation
token. Never broaden the hosted-agent card to make the external-client phase
pass.

## Preconditions

1. Deploy a current Connection Hub package and application plus a current
   KDCube host adapter.
2. Sign in as the test user in KDCube.
3. Connect at least one disposable Slack account or workspace under
   **Delegated to KDCube**.
4. Confirm that the active delegated catalog exposes the resources and
   operations used below.
5. Prepare a conversation whose resident agent can call named services.
6. Prepare a harmless image and a Slack DM or channel controlled by the test
   user.
7. Install Claude Code for the external-client phase.
8. Keep service logs and the KDCube ReAct timeline available.
9. For the external-MCP phase, prepare a disposable streamable-HTTP MCP server
   with one harmless tool, one sibling tool, and controls for changing the
   advertised tool descriptor.
10. For direct admission, prepare the reference protected service and its
    descriptor-owned service registration and signing secret.

For a local KDCube runtime, establish these shell variables with values for
the deployment:

```bash
export KDCUBE_WORKDIR="$HOME/.kdcube/kdcube-runtime/<tenant>__<project>"
export KDCUBE_BASE_URL="https://<runtime-host>"
export KDCUBE_TENANT="<tenant>"
export KDCUBE_PROJECT="<project>"

export NAMED_SERVICES_MCP_URL="$KDCUBE_BASE_URL/api/integrations/bundles/$KDCUBE_TENANT/$KDCUBE_PROJECT/kdcube-services@1-0/public/mcp/named_services"
export PRODUCTIVITY_MCP_URL="$KDCUBE_BASE_URL/api/integrations/bundles/$KDCUBE_TENANT/$KDCUBE_PROJECT/kdcube-services@1-0/public/mcp/productivity"
```

Confirm runtime health before testing:

```bash
kdcube info --workdir "$KDCUBE_WORKDIR"
kdcube bundle status connection-hub@1-0 --workdir "$KDCUBE_WORKDIR" --live
```

If a source fix is under test, first restage and rebuild the runtime using the
host platform's documented source selector. A browser refresh does not load a
new Python package into an already built runtime image.

## Phase 1: Baseline The Hosted Agent Card

1. Open **Connection Hub -> Delegated by KDCube**.
2. Find the card for the resident bundle and agent used by the test
   conversation.
3. Record its `access_id`, revision, expiry, resource, outer KDCube grants,
   named-service operation selection, selected account, and account claims.
4. Confirm the card is distinct from every external-client and automation
   card.
5. In **Delegated to KDCube**, confirm the selected Slack account is connected
   and identify which provider claims it currently exposes.

Expected result: the card is current, the active catalog has no unresolved
drift for the tested capability, and the account selection is unambiguous.

## Phase 2: Exact Operation-Only Live Consent

This phase is the regression test for a demand whose door and account claims
are already present while one exact named-service operation is absent.

1. On the hosted-agent card, retain:
   - the named-services managed resource;
   - the outer `named_services:use` grant;
   - the intended Slack account binding;
   - the provider claims needed by the harmless operation.
2. Remove only `slack.object.schema` from the card's selected named-service
   operations. Save the card and record its new revision.
3. Ask the agent to read the Slack object schema now. Require it to make the
   real `named_services.object_schema` call.
4. Inspect the structured denial.

Expected denial:

- code: `delegated_capability_not_granted`;
- requested capability kind: `named_service_operation`;
- namespace: `slack`;
- operation: `object.schema`;
- the consent demand may contain `claims: []` because all account and door
  claims are already satisfied;
- recovery requests the exact missing operation and does not ask for unrelated
  capabilities.

5. Confirm the chat displays one actionable consent banner.
6. Click **Grant access** once.

Expected navigation and selection:

- Connection Hub opens directly on **Delegated by KDCube**;
- the correct hosted-agent card and request editor are visible on the first
  click;
- the requested service operation is expanded and marked as proposed;
- existing persisted grants are visibly distinguished from pending choices;
- account claims and service operations remain separate sections;
- no unrelated account or operation is selected automatically.

7. Grant the exact pending operation while the agent turn is still active.

Expected live behavior:

- the card revision increments;
- a `connections.consent.granted` event is published even though the demand's
  claim set is empty;
- the active turn may fold the event into its live lane;
- the grant event informs the agent that authority changed but never executes
  the denied operation by itself.

8. Retry the same tool invocation in the active turn if the agent is still
   waiting. If the turn completed, send one follow-up asking it to retry.

Expected result: `slack.object.schema` succeeds. A different ungranted Slack
operation remains denied.

9. Hard-refresh the host page after a denial and before granting once during
   this phase.

Expected result: the actionable consent state survives the refresh and its
first **Grant access** click still opens the exact pending request.

## Phase 3: Prove Operation And Account Claims Are Independent

Use `slack.object.action.upload_file` because it requires both an exact
named-service operation and the account claim `slack:files:write`.

### 3A. Account claim missing

1. Select `slack.object.action.upload_file` on the hosted-agent card.
2. Remove `slack:files:write` only from the selected Slack account permissions
   on that card.
3. Ask the agent to upload the harmless image.

Expected result: the operation selection is present, but admission fails for
the connected-account permission. The banner routes the user to the relevant
account and provider claim rather than proposing a second copy of the service
operation.

### 3B. Exact operation missing

1. Restore `slack:files:write` on the selected account.
2. Remove only `slack.object.action.upload_file` from the service operation
   selection.
3. Retry the upload.

Expected result: the denial is operation-only, identifies
`slack.object.action.upload_file`, and may carry `claims: []`. Granting it adds
only that operation.

### 3C. No connected account

Use a separate test card or temporarily remove the Slack account binding.
Request the same upload operation.

Expected result: Connection Hub routes the user to **Delegated to KDCube** to
connect or choose an account. It does not present an operation-only grant as a
substitute for an account.

### 3D. Multiple accounts

If two Slack accounts are connected, repeat the grant flow.

Expected result: each account is shown separately, the requested claim is
visible per account, and the user explicitly chooses which account the card
may use. A claim on one account never authorizes the other.

## Phase 4: Real Slack Image And Message Workflow

Grant the hosted-agent card exactly these capabilities for the selected test
account:

- outer grant: `named_services:use`;
- `slack.object.schema`;
- `slack.object.action.upload_file`;
- `slack.object.action.post_message`;
- account claims `slack:files:write` and `slack:post`.

Then run this user workflow:

1. Attach or reference the harmless image in the conversation.
2. Ask the agent to send the image and a unique reminder text to the disposable
   Slack DM or channel.
3. Require the agent to inspect schema when it does not know the upload and
   message payload contract.
4. Require a real upload followed by a real post operation.

Expected result:

- the schema call succeeds;
- the upload returns a real Slack file reference and a nonzero valid image;
- the post operation succeeds;
- Slack displays both the expected image and the unique reminder text;
- no provider token or Connection Hub bearer appears in the model timeline,
  payload, or logs.

Text arriving without the image is a failure. A generated placeholder or an
invalid image accepted as an artifact is a failure.

## Phase 5: Revocation Applies To The Next Invocation

1. After a successful Slack upload, remove only
   `slack.object.action.upload_file` from the card.
2. Without restarting the conversation, request a second upload.

Expected result: the second invocation is denied immediately. Authority is
resolved per invocation, so a previous successful tool call does not authorize
the next one.

3. Restore the operation and verify one successful upload.
4. Revoke the entire hosted-agent card.
5. Request another named-service operation.

Expected result: the next invocation fails because the exact card is revoked.
It never falls through to application authority or another card.

Restore or recreate the hosted-agent card before continuing.

## Phase 6: Managed MCP Surfaces Beyond Named Services

The delegated-card system governs any registered managed resource. Named
services add an inner namespace/operation decision; they are not the only
consumer.

For each available managed surface, use a separate harmless read operation:

| Surface | Suggested proof |
| --- | --- |
| Conversation MCP | Grant `conversations:read`, export or read one conversation, revoke it, and prove the next request is denied. |
| Productivity MCP | Grant one declared tool plus its selected account claims, call it, remove the tool or claim independently, and prove the matching denial. |
| Application-defined MCP | Register a test resource and read operation in an app bundle, grant only that operation, and prove a sibling operation remains denied. |
| Application-defined REST | Protect a harmless read endpoint with a registered resource/operation, call it with the card bearer, revoke the operation, and prove the next call is denied. |

For every surface, capture the exact resource and outer tool/operation in the
card. An app-defined capability must behave exactly like a platform-defined
capability: complete active-catalog ceiling, exact current card, structured
denial, and immediate next-invocation revocation.

For one MCP surface, remove only the selected outer tool while retaining its
resource and required claims. Invoke that tool from the hosted agent. The
structured denial and focused Connection Hub card must name that exact outer
operation. Grant it once, confirm the card revision increases, and retry the
tool successfully. The grant must author `connections.consent.granted` for
the matching conversation without replaying the call.

Also register or use two protected resources that expose the same tool name.
Select the tool on resource A only. A call to resource A must succeed and the
same-named call to resource B must return
`delegated_capability_not_granted`. This is the acceptance proof that
`resource_operations` is authority and the flat `operations` union is only a
compatibility projection.

## Phase 6A: User-Owned External MCP Proxy

This phase covers an MCP service that does not integrate with Connection Hub.
Connection Hub owns discovery, card projection, admission, credential
injection, and upstream dispatch.

1. Open **External MCP** and create a connector to the disposable server. Use
   its bearer or custom-header credential when the fixture requires one.
2. Inspect the returned connector. Record its connector revision, descriptor
   revision, resource, server metadata, and accepted tool names.
3. Confirm the response and durable connector record contain only
   `credential_present` and an opaque secret reference. The credential value
   must not appear in the browser response, card, connector revision, or logs.

Before granting the connector, run the network-negative fixture. Confirm HTTP,
loopback, private, link-local, non-global, and mixed public/private DNS answers
are denied. Use a rebinding fixture that returns a public address during URL
validation and a private address when the socket opens; the connect-time guard
must deny it without attempting that address. Confirm redirects and inherited
HTTP proxy environment settings are not followed by the connector transport.

4. Configure an OAuth-capable MCP client with
   `connection-hub@1-0/public/mcp/remote_mcp_proxy` as its protected resource
   and start login. Do not create or paste a delegated bearer for this path.
5. Before login, confirm discovery reveals no owner connector inventory. After
   platform login, confirm consent shows the authenticated owner's connector
   and its exact accepted tools, while another owner's connector is absent.
   Select only the harmless tool and approve.
6. Inspect the resulting OAuth card. It must name the proxy resource and the
   exact connector resource, grant only the selected tool under that connector,
   and have a client-specific `access_id`. Confirm `tools/list` exposes the
   selected tool and omits the sibling.
7. Invoke the selected tool and verify the real upstream fixture records one
   call. Invoke the sibling by name and verify an exact operation denial.
8. Remove the selected operation from the card, invoke it with the same stored
   OAuth session, and follow the
   focused recovery link. Choose **Allow once**. Confirm one retry succeeds,
   a new invocation id is denied, and the card still names the operation.
9. Retry the successful call with the same invocation id and identical
   arguments. Confirm the recorded result returns and the upstream call count
   remains one. Reuse that id with changed arguments and confirm an invocation
   id conflict.
10. Choose **Allow always** and confirm two new invocation ids succeed.
11. Change the fixture's advertised schema or description for the selected
    tool. Refresh the connector. Confirm drift names that tool and the call is
    denied before a one-use permit is consumed.
12. Accept the pending descriptor. Confirm the descriptor revision advances.
    A newly added tool appears in the card editor unchecked and remains denied
    until explicitly granted.
13. Disable the connector and verify the next call is denied. Re-enable it,
    revoke the caller card, and verify there is no fallback to another card.
14. Delete the disposable connector and confirm its upstream credential is
    removed from the secret store.

Repeat the initial `tools/list` and one harmless call with a separate manually
issued card and bearer when the release also needs coverage of the manual
credential path. The OAuth run is the primary external-client proof; the manual
run is an independent issuance and custody test.

## Phase 6B: Direct Protected-Service Admission

This phase covers a backend that integrates with Connection Hub but executes
its own domain operation.

1. Register the reference service id, signing-secret reference, and exact
   resource selector in the Connection Hub descriptor. Publish the resource,
   operations, and grants in the active delegated catalog.
2. Give a dedicated caller card the resource and one harmless operation.
3. Send the bearer to the reference service. Have the service sign a fresh
   admission request containing the semantic operation, a stable invocation
   id, and a digest of its domain request.
4. First send an invalid workload signature. Confirm denial happens before
   bearer/card details are evaluated or disclosed.
5. Send the valid request. Confirm the provider receives a `prk_sub_...` user
   id and `prk_client_...` caller-profile id, plus bounded authority. It must
   not receive the platform user id, raw caller client id, card `access_id`,
   bearer, or provider credential.
6. Repeat for the same user and caller profile at the same service. Confirm
   both pairwise ids are stable. Use another caller profile and confirm only
   the pairwise caller-profile id changes. Use another registered service and
   confirm neither id correlates across services.
7. Remove the operation and call it. Confirm the denial names the exact card,
   resource, and operation and offers **Allow once** and **Allow always**.
8. Choose **Allow once**, retry with one invocation id, and confirm admission
   succeeds. A new invocation id must be denied. Repeating the successful id
   and digest returns the same admission decision.
9. For a state-changing fixture operation, prove the provider's own
   idempotency ledger applies the domain effect once when that same invocation
   id is retried. Connection Hub replays the decision; it cannot record the
   external service's effect.
10. Choose **Allow always** and confirm two new invocation ids are admitted.
11. Revoke the exact card and confirm the next admission is denied. Remove or
    disable the service registration and confirm workload authentication or
    resource registration fails closed.

## Phase 7: Claude Code Consent To KDCube

Yes, Claude Code should be tested. It proves external-client OAuth, exact
bearer-to-card binding, per-client authority, and current revocation. Use a
dedicated connector name and card.

### 7A. Add and authenticate the client

Remove a stale local registration, then add the named-services MCP endpoint:

```bash
claude mcp remove --scope local kdcube-named-services-e2e
claude mcp add --scope local --transport http kdcube-named-services-e2e "$NAMED_SERVICES_MCP_URL"
claude
```

Inside Claude Code, run `/mcp`, select `kdcube-named-services-e2e`, and finish
the browser authorization flow as the test user.

Expected result:

- Connection Hub creates or selects one card bound to this external client;
- the card has its own `access_id`, caller label, expiry, resource, and selected
  capabilities;
- it is not the hosted-agent card;
- the bearer/session resolves to this exact card on every request.

### 7B. Minimal grant and denial

1. Grant one harmless schema or list operation to the Claude Code card.
2. Invoke it from Claude Code and verify success.
3. Invoke a sibling operation that was not granted.

Expected result: the sibling operation is denied with a structured exact
capability reason. It does not inherit authority from the hosted-agent card or
another external client.

### 7C. Extend, revoke, and retry

1. Add the denied operation only to the Claude Code card.
2. Retry from the same Claude Code process; verify success without reconnecting.
3. Remove the operation and retry; verify denial on the next invocation.
4. Regrant it, verify success, then revoke the entire Claude Code card.
5. Retry from the still-running Claude Code process.

Expected result: card changes take effect on the next request. Revocation does
not require an agent restart, and the bearer cannot resolve to another card.

### 7D. Exact card binding

When practical, register a second external client or second dedicated Claude
Code connector. Give it a different card and capability set.

Expected result: each bearer resolves only its own `access_id`. Revoking one
does not affect the other, and neither can use the hosted-agent card.

Remove the test connector after acceptance:

```bash
claude mcp remove --scope local kdcube-named-services-e2e
```

## Phase 8: Automation Credential

Create a short-lived manual automation card with one resource and one harmless
operation. Exercise it through the real REST or MCP transport.

Verify:

1. the exact granted operation succeeds;
2. a different operation on the same resource is denied;
3. the same operation on a different resource is denied;
4. expiration denies the next request;
5. revocation denies the next request;
6. no user-session or hosted-agent card is used as fallback authority.

Treat the bearer as a secret. Enter it only into the client credential store
or process that needs it; never paste it into the test record.

## Phase 9: Durability And Rebuildable Serving State

1. Record each test card's current revision and active catalog version.
2. Hard-refresh the browser.
3. Restart the KDCube runtime services using the deployment's normal operator
   procedure.
4. Reopen Connection Hub.

Expected result:

- current cards, account selections, revisions, and the active catalog remain;
- revoked cards remain revoked;
- Redis serving projections can be rebuilt from durable Connection Hub state;
- the next real invocation produces the same allow or denial as before the
  restart.

Read Redis and generated files only as diagnostic projections. Do not edit
them to make the test pass.

## Phase 10: Fail-Closed Cases

Run these cases with disposable test records:

| Case | Expected result |
| --- | --- |
| Unknown or revoked `access_id` | Denied; no fallback card. |
| Expired card | Denied before provider invocation. |
| Resource mismatch | Denied even if operation names match. |
| Exact operation mismatch | Denied even if account claims are broad enough. |
| Missing account binding | Actionable connected-account consent, not application authority. |
| Missing provider claim | Actionable per-account claim demand. |
| Operation-only gap | Actionable demand is published when `claims` is empty. |
| External MCP connector disabled or deleted | Proxy call is denied before upstream dispatch. |
| External MCP selected tool changed or removed | Proxy call is denied before invocation-policy consumption. |
| One-use policy consumed | A new invocation id is denied; the successful id and digest replay according to the provider mode. |
| Reused invocation id with changed request | Denied as an idempotency conflict. |
| Prepared card/policy change | Retryable fail-closed denial until the same change id completes. |
| Invalid direct-service proof | Denied before bearer/card information is evaluated or disclosed. |
| Active catalog removed the capability | Denied and surfaced as catalog/card drift; a stale card cannot restore it. |
| Durable authority unavailable | Service error; no implicit allow from stale process memory. |
| Different signed-in user | That user cannot inspect, edit, or use another user's cards or connected accounts. |

## Evidence Checklist

Retain the following for the acceptance report:

- before/after screenshots of the exact card, pending proposal, account claim,
  and persisted grant states;
- one-click consent-banner navigation evidence;
- card `access_id`, revision, and catalog version without credential material;
- structured denial code and requested capability for every negative case;
- `connections.consent.granted` event for both a non-empty-claim demand and an
  operation-only `claims: []` demand;
- structured success result and provider-side effect for Slack;
- Claude Code MCP connection name and success/denial results without tokens;
- external MCP connector and descriptor revisions, accepted/pending drift,
  upstream call counts, and secret-redaction evidence;
- direct-service pairwise user/profile ids, signed-proof denial, and provider
  idempotency evidence without raw identities or credentials;
- once/always policy revision, invocation ids, request digests, and replay
  outcome without request secrets;
- restart timestamps and post-restart invocation result;
- deployment and package revisions tested;
- cleanup confirmation.

For a local KDCube runtime, the ReAct debug material can help correlate a
conversation with grant delivery:

```bash
rg -n 'connections\.consent\.granted|delegated_capability_not_granted|object\.schema|object\.action\.upload_file' "$KDCUBE_WORKDIR/data/react-debug"
```

Filter the result by the recorded conversation ID. Logs are diagnostic
evidence; the real operation result remains the acceptance authority.

## Final Acceptance Checklist

- [ ] Hosted agent resolves its exact current card.
- [ ] Operation-only consent with `claims: []` produces an actionable banner
      and grant event.
- [ ] One banner click opens the exact pending request.
- [ ] Persisted and proposed choices are visually distinguishable.
- [ ] Service operation and connected-account claims are granted independently.
- [ ] Missing account and missing provider claim route to the relevant account.
- [ ] Slack image upload and message both succeed in the real workspace.
- [ ] Revocation affects the next invocation without restarting the agent.
- [ ] Conversation/Productivity or another plain managed MCP surface passes.
- [ ] One application-defined managed capability passes the same allow/deny test.
- [ ] External MCP proxy proves exact tool selection, credential containment,
      descriptor drift, once/always, and no-redispatch replay.
- [ ] Direct admission proves independent workload authentication, pairwise
      user/profile identity, once/always, and provider-owned effect idempotency.
- [ ] Claude Code uses its own card and observes grant and revocation live.
- [ ] Automation credential is resource- and operation-bounded.
- [ ] Cards and catalog survive browser and runtime restart.
- [ ] Every negative case is fail-closed with a structured reason.
- [ ] Test cards, connectors, files, and messages are cleaned up.
