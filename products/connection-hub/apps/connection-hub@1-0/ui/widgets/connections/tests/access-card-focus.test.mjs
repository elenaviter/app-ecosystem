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
  assert.equal(request.controlOnly, false)
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
  assert.equal(request.controlOnly, false)
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

test('control_card_id opens only the credentialless control Card', () => {
  const request = focus({
    control_card_id: 'project-control-card',
    access_id: 'ignored-caller-card',
  })
  assert.equal(request.accessId, 'project-control-card')
  assert.equal(request.manualOnly, false)
  assert.equal(request.controlOnly, true)
  assert.equal(matchesAccessCardFocus(
    { access_id: 'project-control-card', source: 'oauth' },
    request,
  ), false)
  assert.equal(matchesAccessCardFocus(
    { access_id: 'project-control-card', source: 'control' },
    request,
  ), true)
})
