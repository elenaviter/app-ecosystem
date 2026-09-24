import assert from 'node:assert/strict'
import test from 'node:test'

import {
  accessCardFocusFromParams,
  findAccessCardFocus,
  matchesAccessCardFocus,
  unavailableAccessCardMessage,
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

test('project person control focus carries its policy coordinates', () => {
  const request = focus({
    control_card_id: 'person-control-1',
    project_ref: 'work:project:quickstart',
    target_subject: 'platform-user-2',
  })
  assert.equal(request.accessId, 'person-control-1')
  assert.equal(request.projectRef, 'work:project:quickstart')
  assert.equal(request.targetSubject, 'platform-user-2')
})

test('the exact visible card is resolved and an unavailable card is named', () => {
  const request = focus({ access_id: 'oauth-fable-card' })
  const cards = [
    { access_id: 'oauth-other-card', source: 'oauth' },
    { access_id: 'oauth-fable-card', source: 'agent' },
  ]
  assert.equal(findAccessCardFocus(cards, request), cards[1])

  const missing = focus({ access_id: 'oauth-hidden-card' })
  assert.equal(findAccessCardFocus(cards, missing), undefined)
  assert.equal(
    unavailableAccessCardMessage(missing),
    'Card oauth-hidden-card does not exist or is not visible to this account.',
  )
})
