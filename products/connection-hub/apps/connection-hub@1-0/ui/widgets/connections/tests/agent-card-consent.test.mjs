// W1018 A: a hosted agent's consent opens its own Agent Card. Operator, 2026-11-07: "W1018 - A".
import assert from 'node:assert/strict'
import test from 'node:test'

import {
  accountSatisfying,
  agentCardConsentFromParams,
  agentCardResolutionMessage,
  consentProviderFor,
  consentRequirements,
  controlUnavailable,
  newRequestedClaims,
  proposeConsentAccount,
  requestedOperationInControl,
  resolveAgentCard,
  withProposedAccountClaim,
} from '../src/features/delegatedAccess/agentCardConsent.ts'

const CLIENT = 'kdcube-agent:workspace@2026-03-31-13-36:main'
const NAMED = '*/api/integrations/bundles/*/*/kdcube-services@1-0/public/mcp/named_services*'

function request(values) {
  return agentCardConsentFromParams((key) => String(values[key] ?? ''))
}

test('the agent_card_consent marker selects Card consent review', () => {
  // The SDK account-binding link as the W1018 retest received it (rows 3 and 4), with the marker.
  const consent = request({ agent_card_consent: '1', agent_client_id: CLIENT, resource: NAMED, claims: '', account_claim: 'slides:read' })
  assert.deepEqual(consent, {
    clientId: CLIENT, accessId: undefined, resource: NAMED, claims: [], namespace: undefined,
    operation: undefined, outerOperation: undefined, accountId: undefined, accountClaim: 'slides:read',
    accountProviderId: undefined, invocationPolicy: undefined, invocationChangeId: undefined, requestBound: undefined,
    requestDigest: undefined, requestApprovalTicket: undefined, requestCardRevision: undefined,
    requestAuthorityRevision: undefined, approvalContext: undefined,
  })
  assert.equal(request({ agent_card_consent: '1', agent_client_id: CLIENT, resource: NAMED, account_claim: 'slides:read', account_provider_id: 'google' }).accountProviderId, 'google')
  // The same link without the marker is still Card review for a hosted caller (see the caller test below).
  assert.equal(request({ pending_agent_grant: '1', agent_client_id: CLIENT, resource: NAMED }).clientId, CLIENT)
  assert.equal(request({ agent_card_consent: '1', resource: NAMED }), null, 'the agent is required')
  assert.equal(request({ agent_card_consent: '1', agent_client_id: CLIENT }), null, 'the resource is required')
})

test('a hosted caller selects Card review even without the marker; an external client never does', () => {
  // An older hosted link: pending_agent_grant only, no agent_card_consent.
  const markerless = request({ pending_agent_grant: '1', agent_client_id: CLIENT, resource: NAMED, claims: '', account_claim: 'slides:read' })
  assert.equal(markerless.clientId, CLIENT)
  assert.equal(markerless.accountClaim, 'slides:read')
  assert.equal(request({ pending_agent_grant: '1', agent_client_id: 'dcr-cli', resource: NAMED, claims: 'x' }), null, 'external clients keep the pending page')
  assert.equal(request({ agent_client_id: CLIENT, resource: NAMED }), null, 'no request at all without pending_agent_grant or the marker')
})

test('the Card is the viewer\'s single active Card for the agent; a named access_id must be that Card', () => {
  const card = { access_id: 'agent-1', client_id: CLIENT, state: 'active' }
  const other = { access_id: 'agent-2', client_id: 'kdcube-agent:other:main', state: 'active' }
  const base = request({ agent_card_consent: '1', agent_client_id: CLIENT, resource: NAMED })
  assert.deepEqual(resolveAgentCard([card, other], base, true), { state: 'loading' })
  assert.deepEqual(resolveAgentCard([card, other], base, false), { state: 'resolved', card })
  assert.deepEqual(resolveAgentCard([other], base, false), { state: 'refused', reason: 'agent_card_not_found' })
  assert.deepEqual(resolveAgentCard([card, { ...card, access_id: 'agent-3' }], base, false), { state: 'refused', reason: 'agent_card_ambiguous' })
  assert.deepEqual(resolveAgentCard([{ ...card, state: 'revoked' }], base, false), { state: 'refused', reason: 'agent_card_not_found' })
  assert.deepEqual(resolveAgentCard([card, other], { ...base, accessId: 'agent-2' }, false), { state: 'refused', reason: 'agent_card_mismatch' })
  assert.deepEqual(resolveAgentCard([card, other], { ...base, accessId: 'agent-1' }, false), { state: 'resolved', card })
  // An inactive named record is never resolved through its id, even when it is the agent's.
  const old = { ...card, access_id: 'agent-old', state: 'revoked' }
  assert.deepEqual(resolveAgentCard([old, card], { ...base, accessId: 'agent-old' }, false), { state: 'refused', reason: 'agent_card_mismatch' })
  assert.deepEqual(resolveAgentCard([old], { ...base, accessId: 'agent-old' }, false), { state: 'refused', reason: 'agent_card_not_found' })
  // A named id does not bypass two active Cards for the same agent.
  assert.deepEqual(resolveAgentCard([card, { ...card, access_id: 'agent-3' }], { ...base, accessId: 'agent-1' }, false), { state: 'refused', reason: 'agent_card_ambiguous' })
  assert.match(agentCardResolutionMessage('agent_card_mismatch', CLIENT), /does not belong to agent/)
  assert.match(agentCardResolutionMessage('agent_card_ambiguous', CLIENT), /more than one active Card/)
})

test('the proposed account: the named one only if capable, else the sole capable one, else the owner chooses', () => {
  const google1 = { account_id: 'g1', provider_id: 'google', claims: ['slides:read', 'slides:write'] }
  const google2 = { account_id: 'g2', provider_id: 'google', claims: ['slides:read'] }
  const readOnly = { account_id: 'g3', provider_id: 'google', claims: ['drive:read'] }
  const slack = { account_id: 's1', provider_id: 'slack', claims: ['slides:read'] }
  const need = (claims, accountId) => ({ providerId: 'google', claims, accountId })
  assert.deepEqual(proposeConsentAccount([google1, readOnly, slack], need(['slides:read'])), { kind: 'sole', accountId: 'g1' })
  assert.deepEqual(proposeConsentAccount([google1, google2], need(['slides:read'])), { kind: 'choose', candidates: ['g1', 'g2'] })
  assert.deepEqual(proposeConsentAccount([google1, google2], need(['slides:read'], 'g2')), { kind: 'named', accountId: 'g2' })
  // A named account that is absent or cannot do it is never answered by another account.
  assert.deepEqual(proposeConsentAccount([google1, readOnly], need(['slides:read'], 'g3')), { kind: 'named_unavailable', accountId: 'g3' })
  assert.deepEqual(proposeConsentAccount([google1], need(['slides:read'], 'gone')), { kind: 'named_unavailable', accountId: 'gone' })
  assert.deepEqual(proposeConsentAccount([readOnly], need(['slides:read'])), { kind: 'none' })
  // A two-claim requirement needs one account with both.
  assert.deepEqual(proposeConsentAccount([google1, google2], need(['slides:read', 'slides:write'])), { kind: 'sole', accountId: 'g1' })
  // Satisfied already: the named account only, when named.
  const held = { google: { g1: ['slides:read'], g2: ['slides:read'] } }
  assert.equal(accountSatisfying(held, need(['slides:read'])), 'g1')
  assert.equal(accountSatisfying(held, need(['slides:read'], 'g2')), 'g2')
  assert.equal(accountSatisfying(held, need(['slides:read'], 'g3')), '')
  assert.equal(accountSatisfying(held, need(['slides:read', 'slides:write'])), '')
})

test('the requirements: the explicit account demand, or every requirement of the operation with all its claims', () => {
  const accounts = [{ account_id: 'g1', provider_id: 'google', claims: ['sheets:read', 'sheets:write'] }]
  const accountOnly = request({ agent_card_consent: '1', agent_client_id: CLIENT, resource: NAMED, account_claim: 'sheets:read', account_provider_id: 'google', account_id: 'g1' })
  assert.deepEqual(consentRequirements(accountOnly, [], accounts), [{ providerId: 'google', claims: ['sheets:read'], accountId: 'g1' }])
  const op = request({ agent_card_consent: '1', agent_client_id: CLIENT, resource: NAMED, namespace: 'sheets', operation: 'values.update' })
  assert.deepEqual(
    consentRequirements(op, [{ providerId: 'google', claims: ['sheets:read', 'sheets:write'] }, { providerId: 'slack', claims: ['chat:write'] }], accounts),
    [{ providerId: 'google', claims: ['sheets:read', 'sheets:write'] }, { providerId: 'slack', claims: ['chat:write'] }],
  )
})

test('a claims-only request adds its new door claims; a fail-closed Control blocks approval', () => {
  assert.deepEqual(newRequestedClaims(['named_services:use'], ['memories:read', 'named_services:use']), ['memories:read'])
  assert.deepEqual(newRequestedClaims(['memories:read'], ['memories:read']), [])
  assert.equal(controlUnavailable(undefined), false)
  assert.equal(controlUnavailable({ state: 'not_controlled' }), false)
  assert.equal(controlUnavailable({ state: 'active' }), false)
  assert.equal(controlUnavailable({ state: 'active', fail_closed: true }), true)
  assert.equal(controlUnavailable({ state: 'unavailable' }), true)
})

test('the provider comes from the capability requirement, else from the only provider holding the claim', () => {
  const accounts = [{ account_id: 'g1', provider_id: 'google', claims: ['slides:read'] }]
  assert.equal(consentProviderFor('slides:read', [{ providerId: 'google', claims: ['slides:read'] }], []), 'google')
  assert.equal(consentProviderFor('slides:read', [], accounts), 'google', 'an account-only request has no capability')
  assert.equal(consentProviderFor('slides:read', [], [], 'google'), 'google', 'account_provider_id names it for an account-only demand')
  assert.equal(consentProviderFor('slides:read', [], [...accounts, { account_id: 'x', provider_id: 'other', claims: ['slides:read'] }]), '')
})

test('a proposal adds the account claim to the Card scope and never removes one', () => {
  const scope = { google: { g1: ['drive:read'] } }
  assert.deepEqual(withProposedAccountClaim(scope, 'google', 'g1', 'slides:read'), { google: { g1: ['drive:read', 'slides:read'] } })
  assert.deepEqual(withProposedAccountClaim(scope, 'google', 'g2', 'slides:read'), { google: { g1: ['drive:read'], g2: ['slides:read'] } })
  assert.equal(withProposedAccountClaim(scope, 'google', 'g1', 'drive:read'), scope)
  assert.equal(withProposedAccountClaim(scope, '', 'g1', 'slides:read'), scope)
})

test('a requested operation is offered only inside the agent\'s Control', () => {
  const control = { slides: ['provider.about', 'object.list'] }
  assert.equal(requestedOperationInControl(control, 'slides', 'object.list'), true)
  // W1018 finding 1: provider.capabilities was outside the Control and was still preselected.
  assert.equal(requestedOperationInControl(control, 'slides', 'provider.capabilities'), false)
  assert.equal(requestedOperationInControl({ slides: ['*'] }, 'slides', 'provider.capabilities'), true)
  assert.equal(requestedOperationInControl(control, undefined, undefined), true, 'an account-only request names no operation')
  assert.equal(requestedOperationInControl(null, 'slides', 'provider.capabilities'), true, 'no Control binds the Card')
})

test('the Control ceiling for one resource: none bound, fail closed, a list, or every operation', async () => {
  const { controlNamedOperations, operationInControl } = await import('../src/features/delegatedAccess/agentCardConsent.ts')
  assert.equal(controlNamedOperations(undefined, NAMED), null)
  assert.equal(controlNamedOperations({ state: 'not_controlled' }, NAMED), null)
  assert.deepEqual(controlNamedOperations({ state: 'unavailable', fail_closed: true }, NAMED), {}, 'a Control that cannot be read offers nothing')
  assert.deepEqual(
    controlNamedOperations({ state: 'active', fail_closed: true, control_authority: { named_service_operations: { [NAMED]: { slides: ['object.list'] } } } }, NAMED),
    {}, 'an active but fail-closed Control offers nothing',
  )
  const active = { state: 'active', control_authority: { named_service_operations: { [NAMED]: { slides: ['provider.about', 'object.list'] } } } }
  assert.deepEqual(controlNamedOperations(active, NAMED), { slides: ['provider.about', 'object.list'] })
  assert.equal(operationInControl(controlNamedOperations(active, NAMED), 'slides', 'provider.capabilities'), false)
  assert.equal(operationInControl(controlNamedOperations(active, NAMED), 'slides', 'object.list'), true)
  assert.equal(operationInControl(controlNamedOperations({ state: 'active', control_authority: { named_service_operations: '*' } }, NAMED), 'slides', 'x'), true)
  assert.equal(operationInControl({}, 'slides', 'object.list'), false, 'fail closed')
})

test('Approve sends one grant on the resolved Card: held authority kept, request and exact account scope added', async () => {
  const { agentCardConsentGrant } = await import('../src/features/delegatedAccess/agentCardConsent.ts')
  const card = {
    access_id: 'agent-1', client_id: CLIENT, card_revision: 7,
    resource_grants: { [NAMED]: ['named_services:use', 'slides:read'] },
    named_service_operations: { [NAMED]: { slides: ['object.list'], docs: ['object.get'] } },
    account_scope: { google: { g0: ['drive:read'] } },
  }
  const scope = { google: { g0: ['drive:read'], g1: ['slides:read'] } }
  // Account-only (the reported link): no operation, no new door claim.
  const accountOnly = request({ agent_card_consent: '1', agent_client_id: CLIENT, resource: NAMED, account_claim: 'slides:read' })
  assert.deepEqual(agentCardConsentGrant({ card, request: accountOnly, requiredDoorGrants: [], operationSelected: false, accountScope: scope }), {
    clientId: CLIENT, accessId: 'agent-1', resource: NAMED, claims: ['named_services:use', 'slides:read'],
    accountScope: scope, expectedCardRevision: 7,
  })
  // With an operation inside the Control: its door grants and the operation are added, the other namespaces kept.
  const withOp = { ...accountOnly, namespace: 'slides', operation: 'object.upsert' }
  const grant = agentCardConsentGrant({ card, request: withOp, requiredDoorGrants: ['slides:write'], operationSelected: true, accountScope: scope })
  assert.deepEqual(grant.claims, ['named_services:use', 'slides:read', 'slides:write'])
  assert.deepEqual(grant.namedServiceOperations, { slides: ['object.list', 'object.upsert'], docs: ['object.get'] })
  // Not selected (outside the Control, or unticked): nothing about the operation is sent.
  const without = agentCardConsentGrant({ card, request: withOp, requiredDoorGrants: ['slides:write'], operationSelected: false, accountScope: scope })
  assert.equal('namedServiceOperations' in without, false)
  assert.deepEqual(without.claims, ['named_services:use', 'slides:read'])
})

test('a refusal names what was outside the Control only through bounded identifiers', async () => {
  const { outsideControlText } = await import('../src/features/delegatedAccess/agentCardConsent.ts')
  const label = (resource) => (resource === NAMED ? 'Named services' : resource)
  assert.equal(
    outsideControlText({ outside: [{ resource: NAMED, namespace: 'slides', operation: 'provider.capabilities' }] }, label),
    "Not allowed by this agent's Control: slides.provider.capabilities on Named services.",
  )
  assert.equal(outsideControlText({ outside: [{ grant: 'slides:write' }] }, label), "Not allowed by this agent's Control: slides:write.")
  assert.equal(
    outsideControlText({ outside: [{ resource: NAMED, namespace: 'slides', operation: 'object.upsert' }], outside_count: 40, outside_truncated: true }, label),
    "Not allowed by this agent's Control: slides.object.upsert on Named services, and more.",
  )
  assert.equal(outsideControlText({ outside: [{ operation: 'x y <script>' }] }, label), '', 'unbounded text is never shown')
  assert.equal(outsideControlText(undefined, label), '')
})

test('an outer operation keeps its once/always choice and request-bound proof in the one Card approval', async () => {
  const { agentCardOuterGrant, outerPolicyState, outerOperationInControl } = await import('../src/features/delegatedAccess/agentCardConsent.ts')
  const RESOURCE = 'https://service.example/mcp'
  const card = {
    access_id: 'agent-1', client_id: CLIENT, card_revision: 4,
    resource_grants: { [RESOURCE]: ['records:read'] }, resource_operations: { [RESOURCE]: ['records_list'] },
    account_scope: {},
  }
  const base = { agent_card_consent: '1', agent_client_id: CLIENT, resource: RESOURCE, outer_operation: 'records_export' }
  const scope = { google: { g1: ['drive:read'] } }

  // No policy asked: the operation joins the held ones.
  const plain = request(base)
  assert.equal(outerPolicyState(plain), 'none')
  assert.deepEqual(agentCardOuterGrant({ card, request: plain, requiredDoorGrants: [], operationSelected: true, accountScope: scope, mode: null }), {
    clientId: CLIENT, accessId: 'agent-1', resource: RESOURCE, claims: ['records:read'], accountScope: scope,
    expectedCardRevision: 4, resourceOperations: ['records_list', 'records_export'],
  })

  // The owner chooses: the existing focused grant, exactly that operation, its mode and change id, on the resolved Card.
  const choose = request({ ...base, invocation_policy: 'choose', invocation_change_id: 'consent-1' })
  assert.equal(outerPolicyState(choose), 'choose')
  const once = agentCardOuterGrant({ card, request: choose, requiredDoorGrants: [], operationSelected: true, accountScope: scope, mode: 'once' })
  assert.equal(once.accessId, 'agent-1')
  assert.equal(once.invocationMode, 'once')
  assert.equal(once.invocationChangeId, 'consent-1')
  assert.deepEqual(once.resourceOperations, ['records_export'], 'exactly one operation carries the policy')
  assert.deepEqual(once.accountScope, scope)
  assert.equal(once.expectedCardRevision, 4)
  assert.equal(agentCardOuterGrant({ card, request: choose, requiredDoorGrants: [], operationSelected: true, accountScope: scope, mode: 'always' }).invocationMode, 'always')

  // Request-bound: the signed ticket and its revisions travel unchanged; nothing is inferred from banner fields.
  const bound = request({ ...base, invocation_policy: 'once', invocation_change_id: 'consent-2', request_bound: '1',
    request_digest: 'abc', request_approval_ticket: 'ticket-1', request_card_revision: '4', request_authority_revision: 'r9',
    approval_application_id: 'app-1' })
  assert.equal(outerPolicyState(bound), 'preset')
  const sent = agentCardOuterGrant({ card, request: bound, requiredDoorGrants: [], operationSelected: true, accountScope: scope, mode: 'once' })
  assert.equal(sent.requestBound, true)
  assert.equal(sent.requestApprovalTicket, 'ticket-1')
  assert.equal(sent.requestDigest, 'abc')
  assert.equal(sent.requestCardRevision, 4)
  assert.equal(sent.requestAuthorityRevision, 'r9')
  assert.deepEqual(sent.approvalContext, { application_id: 'app-1' })

  // A policy asked without a change id cannot be committed: Approve stays disabled.
  assert.equal(outerPolicyState(request({ ...base, invocation_policy: 'choose' })), 'missing')

  // Ceiling for outer operations: none bound allows, unavailable allows nothing, an active Control decides.
  assert.equal(outerOperationInControl(undefined, RESOURCE, 'records_export'), true)
  assert.equal(outerOperationInControl({ state: 'unavailable' }, RESOURCE, 'records_export'), false)
  // An exact Control snapshot, as the runtime requires before it composes (a non-exact one allows no outer operation).
  const control = { state: 'active', control_authority: {
    resource_operations: { [RESOURCE]: ['records_list'] }, resource_grants: { [RESOURCE]: ['records:read'] },
    named_service_operations: {}, account_scope: {},
    properties: { 'connection_hub.control_snapshot': { schema: 'connection_hub.control_snapshot.v1', mode: 'exact', state: 'exact', basis_catalog_version: 'cat-1' } },
  } }
  assert.equal(outerOperationInControl(control, RESOURCE, 'records_list'), true)
  assert.equal(outerOperationInControl(control, RESOURCE, 'records_export'), false)
})
