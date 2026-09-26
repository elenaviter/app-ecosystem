import assert from 'node:assert/strict'
import test from 'node:test'

import {
  accessCardFocusFromParams,
  findAccessCardFocus,
  matchesAccessCardFocus,
  projectPersonControlNotice,
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

test('pending invitation control focus carries its invitation coordinate', () => {
  const request = focus({
    control_card_id: 'invitation-control-1',
    project_ref: 'work:project:quickstart',
    invitation_ref: 'work:invitation:inv-1',
  })
  assert.equal(request.accessId, 'invitation-control-1')
  assert.equal(request.projectRef, 'work:project:quickstart')
  assert.equal(request.invitationRef, 'work:invitation:inv-1')
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


test('W260: an unavailable Card names Connection Hub\'s reason', () => {
  const request = focus({ control_card_id: 'control-1' })
  const message = unavailableAccessCardMessage(request, 'control_card_not_found.')
  assert.match(message, /does not exist or is not visible/)
  assert.match(message, /Connection Hub answered: control_card_not_found\.$/)
  assert.equal(unavailableAccessCardMessage(request), 'Card control-1 does not exist or is not visible to this account.')
})

test('W260: a My Card asked for as a Control Card says to open it by access_id', () => {
  const asControl = focus({ control_card_id: 'person-my-card-7c43016d992ae974fc9c5f1b' })
  assert.match(unavailableAccessCardMessage(asControl, 'control_card_not_found'), /My Card.*opens with access_id, not control_card_id/)
  const asAccess = focus({ access_id: 'person-my-card-7c43016d992ae974fc9c5f1b' })
  assert.doesNotMatch(unavailableAccessCardMessage(asAccess), /access_id, not control_card_id/)
})

test('W260: a person Control Card is read only here; an admin is sent to Team > People', () => {
  assert.match(projectPersonControlNotice({ edit_in_project: true }), /Team > People/)
  assert.match(projectPersonControlNotice({ edit_in_project: false }), /decided by an admin/)
  assert.match(projectPersonControlNotice(undefined), /decided by an admin/)
})

test('W260: a My Card opens by access_id among the person\'s own Cards', () => {
  const myCard = { access_id: 'person-my-card-7c43016d992ae974fc9c5f1b', source: 'project-person' }
  assert.equal(matchesAccessCardFocus(myCard, focus({ access_id: myCard.access_id })), true)
  // Asked for as a Control Card it can never match: that link is the defect.
  assert.equal(matchesAccessCardFocus(myCard, focus({ control_card_id: myCard.access_id })), false)
})
