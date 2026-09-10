import assert from 'node:assert/strict'
import test from 'node:test'

import { groupClaimsByService, splitClaim } from '../src/components/claimGroups.ts'

test('a token splits at its first colon only', () => {
  assert.deepEqual(splitClaim('canvas:read'), { token: 'canvas:read', service: 'canvas', verb: 'read' })
  assert.deepEqual(splitClaim('slack:files:write'), { token: 'slack:files:write', service: 'slack', verb: 'files:write' })
  assert.deepEqual(splitClaim('all'), { token: 'all', service: 'all', verb: '' })
})

test('groups keep first-seen order and drop duplicates', () => {
  const groups = groupClaimsByService([
    'canvas:read', 'canvas:write', 'docs:read', 'canvas:read', 'named_services:use', 'docs:write',
  ])
  assert.deepEqual(groups.map((g) => g.service), ['canvas', 'docs', 'named_services'])
  assert.deepEqual(groups[0].claims.map((c) => c.verb), ['read', 'write'])
  assert.deepEqual(groups[1].claims.map((c) => c.verb), ['read', 'write'])
  assert.deepEqual(groups[2].claims, [{ token: 'named_services:use', verb: 'use' }])
})
