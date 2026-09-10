import assert from 'node:assert/strict'
import test from 'node:test'

import { groupCards } from '../src/features/delegatedAccess/cardGroups.ts'

const now = 1_800_000_000
const cards = [
  { access_id: 'a', source: 'agent', expires_at: now + 90 * 86400, resource_grants: { 'x/mcp/memories*': ['m:read'] } },
  { access_id: 'b', source: 'oauth', expires_at: now + 3 * 86400, resource_grants: { 'x/mcp/named_services*': ['n:use'] } },
  { access_id: 'c', source: 'manual', expires_at: now - 10, resource_grants: { 'x/mcp/named_services*': ['n:use'] } },
  { access_id: 'd', source: 'manual', expired: true, expires_at: now + 10, resource_grants: {} },
]
const doorLabel = (r) => Object.keys(r.resource_grants || {}).map((k) => k.replace(/^.*\/mcp\//, '').replace(/\*$/, '')).join(', ')
// The state rule lives in grantFilter; here it is the server flag first, then the clock.
const stateOf = (r) => (r.expired ? 'expired' : r.expires_at <= now ? 'expired' : r.expires_at - now <= 7 * 86400 ? 'expiring' : 'active')

test('by kind keeps first-seen order and labels the kinds', () => {
  const groups = groupCards(cards, 'kind', { stateOf, doorLabel })
  assert.deepEqual(groups.map((g) => [g.label, g.records.length]), [['Agents', 1], ['Connected apps', 1], ['Manual tokens', 2]])
})

test('by state trusts the server flag over the clock', () => {
  const groups = groupCards(cards, 'state', { stateOf, doorLabel })
  assert.deepEqual(groups.map((g) => [g.label, g.records.map((r) => r.access_id)]), [
    ['Active', ['a']], ['Expiring soon', ['b']], ['Expired', ['c', 'd']],
  ])
})

test('by door groups on the door label and names the doorless', () => {
  const groups = groupCards(cards, 'door', { stateOf, doorLabel })
  assert.deepEqual(groups.map((g) => [g.key, g.records.length]), [['memories', 1], ['named_services', 2], ['no door', 1]])
})
