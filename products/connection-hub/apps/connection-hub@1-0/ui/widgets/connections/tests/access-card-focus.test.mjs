import assert from 'node:assert/strict'
import test from 'node:test'

import {
  accessCardFocusFromParams,
  findAccessCardFocus,
  isRequestLimitRefusal,
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

test('W260: a project admin edits a person Control Card here; anyone else reads it', () => {
  // Operator ruling 2026-09-26: Connection Hub is the only Card editor.
  assert.equal(projectPersonControlNotice({ can_edit: true }), '')
  assert.match(projectPersonControlNotice({ can_edit: false }), /A project admin decides this Control Card/)
  assert.match(projectPersonControlNotice(undefined), /A project admin decides this Control Card/)
  assert.doesNotMatch(projectPersonControlNotice({ can_edit: false }), /Team > People/)
})

test('W260: a My Card opens by access_id among the person\'s own Cards', () => {
  const myCard = { access_id: 'person-my-card-7c43016d992ae974fc9c5f1b', source: 'project-person' }
  assert.equal(matchesAccessCardFocus(myCard, focus({ access_id: myCard.access_id })), true)
  // Asked for as a Control Card it can never match: that link is the defect.
  assert.equal(matchesAccessCardFocus(myCard, focus({ control_card_id: myCard.access_id })), false)
})

test('W435: a refusal by the request limit says so, and claims nothing about the Card either way', () => {
  const request = focus({ control_card_id: 'person-control-7c43016d992ae974fc9c5f1b' })
  const burst = unavailableAccessCardMessage(request, 'Burst limit exceeded (161/160)')
  assert.equal(burst, 'Card person-control-7c43016d992ae974fc9c5f1b was not loaded: the platform refused the request because this account reached its request limit for one minute (Burst limit exceeded (161/160)). Wait about a minute and try again.')
  const hourly = unavailableAccessCardMessage(request, 'Hourly limit exceeded (601/600).')
  assert.match(hourly, /reached its request limit for the hour \(Hourly limit exceeded \(601\/600\)\)\. Wait and try again later\.$/)
  // An unknown Card refused by the limit: no missing-Card diagnosis and no
  // promise that it exists or will open.
  const unknown = unavailableAccessCardMessage(focus({ access_id: 'oauth-never-existed' }), 'Burst limit exceeded (161/160)')
  for (const message of [burst, hourly, unknown]) {
    assert.doesNotMatch(message, /does not exist|not visible/)
    assert.doesNotMatch(message, /nothing is wrong|to open it|will open|is fine/i)
  }
  assert.equal(isRequestLimitRefusal('Burst limit exceeded (161/160)'), true)
  assert.equal(isRequestLimitRefusal('control_card_not_found'), false)
  assert.match(unavailableAccessCardMessage(request, 'control_card_not_found'), /does not exist or is not visible/)
})

// Live 2026-10-21 (operator, local Hub at 2a9815e5): a board link opened Yaroslav's agent Card through
// KDCube Maintenance; "Open KDCube Maintenance" opened the project Control Card under a red "Card
// unavailable. Card aut_55d0077e95c18b1e does not exist or is not visible to this account." The Control
// Card replaced the agent Card in the one focused slot, and the link's focus then reported it missing.
// Operator: "i should be able to see. i also perfectly should be able to click on the project card linked".
test('opening the linked Control Card from a link-opened Card leaves no unavailable banner', async () => {
  const { unmatchedAccessCardFocusState, linkedControlOpenOutcome } = await import('../src/features/delegatedAccess/accessCardFocus.ts')
  const link = focus({ access_id: 'aut_55d0077e95c18b1e', project_ref: 'work:project:kdcube-maintenance' })
  const agentCard = { access_id: 'aut_55d0077e95c18b1e', source: 'grant' }
  const controlCard = { access_id: 'control-d0cc151b', source: 'control' }
  const target = { controlId: 'control-d0cc151b', projectRef: 'work:project:kdcube-maintenance' }
  const state = (linkedSwitch) => unmatchedAccessCardFocusState({ loading: false, linkedSwitch, accessId: link.accessId })

  // The panel's own lookup: before the open the link finds its Card, after it only the Control Card is held.
  assert.equal(findAccessCardFocus([agentCard], link), agentCard)
  assert.equal(findAccessCardFocus([controlCard], link), undefined)

  // While the read runs the link is loading, never resolved on a Card that is not there.
  assert.equal(state({ from: link.accessId, verified: false }), 'loading')
  // The read answered with exactly the bound Card: the switch is verified and the link is done.
  const opened = linkedControlOpenOutcome(link.accessId, target, { access: controlCard })
  assert.deepEqual(opened, { open: controlCard, restore: false, message: '', linkedSwitch: { from: link.accessId, verified: true } })
  assert.equal(state(opened.linkedSwitch), 'resolved')
  // A Card the link never found is still reported, and a read in flight is still loading.
  assert.equal(state(null), 'unavailable')
  assert.equal(unmatchedAccessCardFocusState({ loading: true, linkedSwitch: opened.linkedSwitch, accessId: link.accessId }), 'loading')
  // Another Card's switch says nothing about this link.
  assert.equal(state({ from: 'aut_other', verified: true }), 'unavailable')
})

// #871 review (codex-app@spark1, 2026-10-22): a refused or mismatched read cleared or replaced the only
// copy of the agent Card while the link already counted as moved on, leaving a blank pane and no error.
test('a refused or mismatched linked read opens nothing, puts the Card back and shows the existing refusal', async () => {
  const { readFileSync } = await import('node:fs')
  const { linkedControlOpenOutcome } = await import('../src/features/delegatedAccess/accessCardFocus.ts')
  const target = { controlId: 'control-d0cc151b', projectRef: 'work:project:kdcube-maintenance' }

  const refused = linkedControlOpenOutcome('aut_55d0077e95c18b1e', target, { error: new Error('control_card_not_found') })
  assert.deepEqual(refused, { open: null, restore: true, message: 'control_card_not_found', linkedSwitch: null })
  assert.equal(linkedControlOpenOutcome('aut_x', target, { error: undefined }).message, 'Failed to load the Control Card')

  const mismatched = linkedControlOpenOutcome('aut_55d0077e95c18b1e', target, { access: { access_id: 'control-other' } })
  assert.deepEqual(mismatched, {
    open: null,
    restore: true,
    message: 'The linked Control Card could not be matched to this Card. Nothing was opened.',
    linkedSwitch: null,
  })
  assert.equal(linkedControlOpenOutcome('aut_x', target, { access: null }).restore, true)

  // The panel applies it: the Card open before the read is kept and put back, the switch is set only from
  // the outcome, and the refusal goes to the granted pane's error line, shown with or without an editor.
  const panel = readFileSync(new URL('../src/features/delegatedAccess/DelegatedAccessPanel.tsx', import.meta.url), 'utf8')
  const open = panel.slice(panel.indexOf('const openLinkedControlCard = async'), panel.indexOf('const renderLeaveDialog'))
  assert.ok(open.indexOf('const before = { card: focusedCard, viewer: focusedViewer }') > -1)
  assert.ok(open.indexOf('const before') < open.indexOf('dispatch(loadControlCard(target))'), 'kept before the read replaces it')
  assert.match(open, /setLinkedSwitch\(\{ from: item\.access_id, verified: false \}\)/)
  assert.match(open, /setLinkedSwitch\(outcome\.linkedSwitch\)/)
  assert.match(open, /if \(outcome\.restore\) dispatch\(restoreFocusedCard\(before\)\)/)
  assert.match(open, /setOpenReadError\(outcome\.message\)/)
  assert.match(open, /if \(outcome\.open\) switchEdit\(outcome\.open\)/)
  assert.doesNotMatch(open, /movedToLinkedControlFrom/)
  assert.match(panel, /\{openReadError \? <div className="error" role="alert">\{openReadError\}<\/div> : null\}/)
  // The focus effect re-runs when the switch changes.
  const effect = panel.slice(panel.indexOf('unmatchedAccessCardFocusState({'))
  assert.match(effect.slice(0, effect.indexOf(']);')), /\n    linkedSwitch,\n/)
})
