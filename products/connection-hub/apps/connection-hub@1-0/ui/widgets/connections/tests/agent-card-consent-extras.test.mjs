// W34: the Agent Card consent review adds more access within the agent's Control, and only adds.
// Narrowing stays in the Card's Edit. A provider permission no account holds is requested after a save.
import assert from 'node:assert/strict'
import test from 'node:test'

import {
  allowedPicks,
  consentExtraGrants,
  consentExtrasOffer,
  grantGroups,
  hasExtraPicks,
  neededProviderClaims,
  NO_EXTRA_PICKS,
  providerUpgradeHref,
  withExtraAccounts,
} from '../src/features/delegatedAccess/consentExtras.ts'

const CLIENT = 'kdcube-agent:workspace@2026-03-31-13-36:main'
const NAMED = '*/api/integrations/bundles/*/*/kdcube-services@1-0/public/mcp/named_services*'
const MEMORIES = '*/api/integrations/bundles/*/*/kdcube-services@1-0/public/mcp/memories*'
const SERVICE = 'https://service.example/mcp'
const SECRETS = 'urn:kdcube:management:secret:*:*:*:*:*'

// A namespace's catalog rows, as DelegatedResourceCatalog.operationRows projects them.
const rowsFor = (namespace) => Object.values(namespace.tools).map((tool) => ({
  operation: tool.operation, label: tool.operation, description: '', grants: tool.grants,
}))
const tool = (operation, grants) => ({ operation, grants })

const resources = [{
  resource: NAMED, label: 'Named services', grants: ['named_services:use', 'slides:read', 'slides:write', 'docs:read'], operations: [],
  named_services: [{
    namespace: 'slides', label: 'Presentations',
    tools: { 'object.get': tool('object.get', ['slides:read']), 'object.upsert': tool('object.upsert', ['slides:write']) },
    connected_accounts: [{ provider_id: 'google', claims: ['slides:read', 'slides:write'],
      claims_by_operation: { 'object.get': ['slides:read'], 'object.upsert': ['slides:read', 'slides:write'] } }],
  }, {
    namespace: 'docs', label: 'Documents',
    tools: { 'object.get': tool('object.get', ['docs:read']), 'object.action.export': tool('object.action.export', ['docs:read']) },
    connected_accounts: [{ provider_id: 'google', connector_app_id: 'gmail', claims: ['docs:read', 'drive:read'],
      claims_by_operation: { 'object.get': ['docs:read'], 'object.action.export': ['docs:read', 'drive:read'] } }],
  }],
}, {
  resource: MEMORIES, label: 'Memories', grants: ['memories:read', 'memories:write'], operations: [],
}, {
  resource: SERVICE, label: 'Records', grants: ['records:read'],
  operations: [{ name: 'records_list', grants: ['records:read'] }, { name: 'records_purge', grants: ['records:read'] }],
}, {
  resource: SECRETS, label: 'Secrets', selector_type: 'kdcube_secret', grants: ['secret:read'], operations: [],
}]

const card = {
  access_id: 'agent-0e150a469bf4edbd', client_id: CLIENT, card_revision: 31,
  resource_grants: { [NAMED]: ['named_services:use', 'slides:read', 'slides:write'] },
  resource_operations: {},
  named_service_operations: { [NAMED]: { slides: ['object.get', 'object.upsert'] } },
  account_scope: {},
}
// The descriptor Control: slides and docs.get inside; docs export, memories:write and records_purge outside.
const control = {
  state: 'active',
  control_authority: {
    resource_grants: { [NAMED]: ['named_services:use', 'slides:read', 'slides:write', 'docs:read'], [MEMORIES]: ['memories:read'], [SERVICE]: ['records:read'] },
    resource_operations: { [SERVICE]: ['records_list'] },
    named_service_operations: { [NAMED]: { slides: ['object.get', 'object.upsert'], docs: ['object.get'] } },
    account_scope: {},
    // An exact Control snapshot, as the runtime composes it (outer operations are read only from one).
    properties: { 'connection_hub.control_snapshot': { schema: 'connection_hub.control_snapshot.v1', mode: 'exact', state: 'exact', basis_catalog_version: 'cat-1' } },
  },
}
const request = { clientId: CLIENT, resource: NAMED, claims: [], accountClaim: 'slides:read', accountProviderId: 'google' }
const offer = consentExtrasOffer(card, control, resources, rowsFor)
const byResource = (resource) => offer.find((item) => item.resource === resource)

test('the offer lists every resource and service the Control allows, held entries fixed and outside entries disabled', () => {
  assert.deepEqual(offer.map((item) => item.resource), [NAMED, MEMORIES, SERVICE], 'a selector row is chosen in the Card editor')
  const named = byResource(NAMED)
  assert.deepEqual(named.namespaces.map((item) => item.namespace), ['docs'], 'a namespace the Card holds whole is not offered again')
  assert.deepEqual(named.namespaces[0].rows.map(({ key, held, inControl }) => ({ key, held, inControl })), [
    { key: 'object.get', held: false, inControl: true },
    { key: 'object.action.export', held: false, inControl: false },
  ])
  // Door grants exclude the provider claims an operation needs on an account (doorGrantsForOperation).
  assert.deepEqual(named.namespaces[0].rows[0].grants, [], 'docs.get brings no door grant beyond the account claim')
  assert.deepEqual(byResource(MEMORIES).grants.map(({ key, inControl }) => ({ key, inControl })), [
    { key: 'memories:read', inControl: true }, { key: 'memories:write', inControl: false },
  ])
  assert.deepEqual(byResource(SERVICE).outer.map(({ key, inControl }) => ({ key, inControl })), [
    { key: 'records_list', inControl: true }, { key: 'records_purge', inControl: false },
  ])
  assert.equal(named.grants.find((row) => row.key === 'slides:read').held, true)
})

test('no Control binds the Card: the catalog is the only bound; an unreadable Control offers nothing', () => {
  const free = consentExtrasOffer(card, { state: 'not_controlled' }, resources, rowsFor)
  assert.ok(free.find((item) => item.resource === NAMED).namespaces[0].rows.every((row) => row.inControl))
  assert.deepEqual(consentExtrasOffer(card, { state: 'active', fail_closed: true, control_authority: control.control_authority }, resources, rowsFor), [])
})

test('only additions inside the Control are kept: a held or outside pick is never sent', () => {
  const picks = {
    grants: { [MEMORIES]: ['memories:read', 'memories:write'], [NAMED]: ['slides:read'] },
    outer: { [SERVICE]: ['records_list', 'records_purge'] },
    named: { [NAMED]: { docs: ['object.get', 'object.action.export'], slides: ['object.get'] } },
    accounts: {},
  }
  assert.deepEqual(allowedPicks(offer, picks), {
    grants: { [MEMORIES]: ['memories:read'] },
    outer: { [SERVICE]: ['records_list'] },
    named: { [NAMED]: { docs: ['object.get'] } },
    accounts: {},
  })
  assert.equal(hasExtraPicks(NO_EXTRA_PICKS), false)
  assert.equal(hasExtraPicks({ ...NO_EXTRA_PICKS, accounts: { google: { a: [] } } }), false)
})

test('one merge grant per resource, each a superset of what the Card holds, door grants following operations', () => {
  const picks = {
    grants: { [MEMORIES]: ['memories:read'] },
    outer: { [SERVICE]: ['records_list'] },
    named: { [NAMED]: { docs: ['object.get'] } },
    accounts: {},
  }
  const grants = consentExtraGrants(card, request, offer, picks)
  assert.deepEqual(grants, [
    { clientId: CLIENT, accessId: card.access_id, resource: MEMORIES, claims: ['memories:read'] },
    { clientId: CLIENT, accessId: card.access_id, resource: SERVICE, claims: ['records:read'], resourceOperations: ['records_list'] },
    {
      clientId: CLIENT, accessId: card.access_id, resource: NAMED,
      claims: ['named_services:use', 'slides:read', 'slides:write'],
      namedServiceOperations: { slides: ['object.get', 'object.upsert'], docs: ['object.get'] },
    },
  ])
  assert.ok(grants.every((grant) => !('replace' in grant)), 'the review never replaces')
  assert.deepEqual(consentExtraGrants(card, request, offer, NO_EXTRA_PICKS), [])
})

test('account permissions are added only where the account holds them, never removed', () => {
  const accounts = [
    { account_id: 'g1', provider_id: 'google', claims: ['slides:read', 'slides:write', 'docs:read'] },
    { account_id: 'g2', provider_id: 'google', claims: ['slides:read'] },
  ]
  const held = { google: { g1: ['slides:read'] } }
  assert.deepEqual(withExtraAccounts(held, { google: { g1: ['slides:write', 'drive:read'], g2: ['slides:write'], g9: ['x'] } }, accounts),
    { google: { g1: ['slides:read', 'slides:write'] } })
  assert.equal(withExtraAccounts(held, {}, accounts), held)
})

test('provider permissions: needed ones are marked where an account holds them; missing ones only for what this consent adds', () => {
  const accounts = [{ account_id: 'g1', provider_id: 'google', claims: ['slides:read', 'docs:read'] }]
  const picks = { ...NO_EXTRA_PICKS, named: { [NAMED]: { docs: ['object.action.export'] } } }
  // Held slides operations need slides:read and slides:write; the picked docs export needs docs:read and drive:read.
  const { bindable, missing } = neededProviderClaims(card, resources, picks, accounts, { google: { g1: ['slides:read'] } }, rowsFor)
  assert.deepEqual(bindable, { google: ['docs:read'] })
  assert.deepEqual(missing, [{ providerId: 'google', connectorAppId: 'gmail', resource: NAMED, claims: ['drive:read'] }],
    'slides:write, needed by what the Card already held, is not this consent\'s question')
  const none = neededProviderClaims(card, resources, NO_EXTRA_PICKS, [], {}, rowsFor)
  assert.deepEqual(none.missing, [], 'nothing picked: nothing to request at a provider')
})

test('permissions are grouped by service, each group on its own', () => {
  const rows = ['slack:post', 'docs:read', 'slack:files:read', 'named_services:use'].map((key) => ({ key, label: key, held: false, inControl: true, grants: [key] }))
  assert.deepEqual(grantGroups(rows).map((group) => [group.service, group.rows.map((row) => row.key)]), [
    ['docs', ['docs:read']], ['named_services', ['named_services:use']], ['slack', ['slack:post', 'slack:files:read']],
  ])
})

test('the provider link carries the agent hand-off and drops the Card review params', () => {
  const base = `https://hub.example/connections?tab=delegated_by_kdcube&agent_card_consent=1&pending_agent_grant=1&agent_client_id=${encodeURIComponent(CLIENT)}&account_claim=slides%3Aread&namespace=slides&keep=1`
  const href = new URL(providerUpgradeHref(base, { providerId: 'google', connectorAppId: 'gmail', resource: NAMED, claims: ['drive:read', 'docs:read'] },
    CLIENT, [{ account_id: 'g1', provider_id: 'google', claims: [] }]))
  assert.equal(href.searchParams.get('tab'), 'delegated_to_kdcube')
  assert.equal(href.searchParams.get('provider_id'), 'google')
  assert.equal(href.searchParams.get('connector_app_id'), 'gmail')
  assert.equal(href.searchParams.get('claims'), 'drive:read,docs:read')
  assert.equal(href.searchParams.get('account_id'), 'g1', 'the only account of the provider is the one extended')
  assert.equal(href.searchParams.get('agent_client_id'), CLIENT)
  assert.equal(href.searchParams.get('agent_resource'), NAMED)
  assert.equal(href.searchParams.get('keep'), '1')
  for (const key of ['agent_card_consent', 'pending_agent_grant', 'account_claim', 'namespace']) assert.equal(href.searchParams.has(key), false, key)
  const several = new URL(providerUpgradeHref(base, { providerId: 'google', connectorAppId: '', resource: NAMED, claims: ['drive:read'] },
    CLIENT, [{ account_id: 'g1', provider_id: 'google' }, { account_id: 'g2', provider_id: 'google' }]))
  assert.equal(several.searchParams.has('account_id'), false, 'several accounts: the owner picks at the provider step')
  assert.equal(several.searchParams.has('connector_app_id'), false)
  assert.equal(providerUpgradeHref('not a url', { providerId: 'google', connectorAppId: '', resource: NAMED, claims: ['x'] }, CLIENT, []), '')
})
