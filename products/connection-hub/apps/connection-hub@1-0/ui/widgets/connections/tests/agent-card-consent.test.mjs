// W1018 A: a hosted agent's consent opens its own Agent Card. Operator, 2026-11-07: "W1018 - A".
import assert from 'node:assert/strict'
import test from 'node:test'

import {
  agentCardConsentFromParams,
  agentCardResolutionMessage,
  consentProviderFor,
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

test('only an explicit agent_card_consent marker selects Card consent review', () => {
  // The SDK account-binding link as the W1018 retest received it (rows 3 and 4), with the marker.
  const consent = request({ agent_card_consent: '1', agent_client_id: CLIENT, resource: NAMED, claims: '', account_claim: 'slides:read' })
  assert.deepEqual(consent, {
    clientId: CLIENT, accessId: undefined, resource: NAMED, claims: [], namespace: undefined,
    operation: undefined, outerOperation: undefined, accountId: undefined, accountClaim: 'slides:read',
    accountProviderId: undefined,
  })
  assert.equal(request({ agent_card_consent: '1', agent_client_id: CLIENT, resource: NAMED, account_claim: 'slides:read', account_provider_id: 'google' }).accountProviderId, 'google')
  // The same link without the marker stays a plain pending grant (older links keep their page).
  assert.equal(request({ pending_agent_grant: '1', agent_client_id: CLIENT, resource: NAMED }), null)
  assert.equal(request({ agent_card_consent: '1', resource: NAMED }), null, 'the agent is required')
  assert.equal(request({ agent_card_consent: '1', agent_client_id: CLIENT }), null, 'the resource is required')
})

test('the Card is resolved from the viewer\'s own Cards; a named access_id must be that agent\'s', () => {
  const card = { access_id: 'agent-1', client_id: CLIENT, state: 'active' }
  const other = { access_id: 'agent-2', client_id: 'kdcube-agent:other:main', state: 'active' }
  const base = request({ agent_card_consent: '1', agent_client_id: CLIENT, resource: NAMED })
  assert.deepEqual(resolveAgentCard([card, other], base, true), { state: 'loading' })
  assert.deepEqual(resolveAgentCard([card, other], base, false), { state: 'resolved', card })
  assert.deepEqual(resolveAgentCard([other], base, false), { state: 'refused', reason: 'agent_card_not_found' })
  assert.deepEqual(
    resolveAgentCard([card, { ...card, access_id: 'agent-3' }], base, false),
    { state: 'refused', reason: 'agent_card_ambiguous' },
    'two Cards for one agent are never guessed between',
  )
  assert.deepEqual(resolveAgentCard([{ ...card, state: 'revoked' }], base, false), { state: 'refused', reason: 'agent_card_not_found' })
  const named = { ...base, accessId: 'agent-2' }
  assert.deepEqual(resolveAgentCard([card, other], named, false), { state: 'refused', reason: 'agent_card_mismatch' })
  assert.deepEqual(resolveAgentCard([card, other], { ...base, accessId: 'agent-1' }, false), { state: 'resolved', card })
  assert.match(agentCardResolutionMessage('agent_card_mismatch', CLIENT), /does not belong to agent/)
  assert.match(agentCardResolutionMessage('agent_card_ambiguous', CLIENT), /more than one active Card/)
})

test('the proposed account: the named capable one, else the sole capable one, else the owner chooses', () => {
  const google1 = { account_id: 'g1', provider_id: 'google', claims: ['slides:read', 'slides:write'] }
  const google2 = { account_id: 'g2', provider_id: 'google', claims: ['slides:read'] }
  const readOnly = { account_id: 'g3', provider_id: 'google', claims: ['drive:read'] }
  const slack = { account_id: 's1', provider_id: 'slack', claims: ['slides:read'] }
  assert.deepEqual(proposeConsentAccount([google1, readOnly, slack], 'google', 'slides:read'), { kind: 'sole', accountId: 'g1' })
  assert.deepEqual(proposeConsentAccount([google1, google2], 'google', 'slides:read'), { kind: 'choose', candidates: ['g1', 'g2'] })
  assert.deepEqual(proposeConsentAccount([google1, google2], 'google', 'slides:read', 'g2'), { kind: 'named', accountId: 'g2' })
  assert.deepEqual(
    proposeConsentAccount([google1, readOnly], 'google', 'slides:read', 'g3'),
    { kind: 'sole', accountId: 'g1' },
    'a named account that cannot do the claim is not proposed',
  )
  assert.deepEqual(proposeConsentAccount([readOnly], 'google', 'slides:read'), { kind: 'none' })
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
  assert.equal(outsideControlText({ outside: [{ operation: 'x y <script>' }] }, label), '', 'unbounded text is never shown')
  assert.equal(outsideControlText(undefined, label), '')
})
