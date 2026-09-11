import assert from 'node:assert/strict'
import test from 'node:test'

import {
  accessCardFocusFromParams,
  matchesAccessCardFocus,
} from '../src/features/delegatedAccess/accessCardFocus.ts'

function focus(values) {
  return accessCardFocusFromParams((key) => String(values[key] || ''))
}

test('access_id focuses any delegated card family', () => {
  const request = focus({ access_id: 'oauth-worker-card' })
  assert.equal(request.manualOnly, false)
  assert.equal(matchesAccessCardFocus(
    { access_id: 'oauth-worker-card', source: 'oauth' },
    request,
  ), true)
})

test('manual_access_id preserves the existing manual-card-only route', () => {
  const request = focus({
    manual_access_id: 'issued-token',
    access_id: 'ignored-shared-id',
    claims: 'docs:read, docs:write',
  })
  assert.equal(request.accessId, 'issued-token')
  assert.equal(request.manualOnly, true)
  assert.deepEqual(request.claims, ['docs:read', 'docs:write'])
  assert.equal(matchesAccessCardFocus(
    { access_id: 'issued-token', source: 'oauth' },
    request,
  ), false)
  assert.equal(matchesAccessCardFocus(
    { access_id: 'issued-token', source: 'manual' },
    request,
  ), true)
})
