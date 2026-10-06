// W587 follow-up C (EMain 15:54): while the board reloaded, the permission check could not answer
// and an admin was shown "a project admin decides this" with Edit and Save gone. An unanswered check
// is neither a refusal nor a yes: Edit stays (it re-reads, so it re-checks), Save waits.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  cardPermissionUnknown, cardReadOnlyReason, cardSaveBlockedReason, PERMISSION_UNKNOWN_MESSAGE,
} from '../src/features/delegatedAccess/cardEditability.ts'

const personControl = {
  access_id: 'person-control-1', source: 'control',
  properties: { 'connection_hub.project_person_control': {
    schema: 'connection_hub.project_person_control.v1', project_ref: 'work:project:one', target_subject: 'person-1' } },
}
const panel = readFileSync(new URL('../src/features/delegatedAccess/DelegatedAccessPanel.tsx', import.meta.url), 'utf8')

test('an admin may edit; a refused viewer reads only', () => {
  assert.equal(cardReadOnlyReason(personControl, { can_edit: true }), '')
  assert.equal(cardSaveBlockedReason(personControl, { can_edit: true }), '')
  assert.match(cardReadOnlyReason(personControl, { can_edit: false }), /A project admin decides this Control Card/)
})

test('an unanswered check is not a refusal: Edit stays, Save waits, and the notice says why', () => {
  const unknown = { can_edit: null, reason: 'project_person_control_permission_unavailable', retryable: true }
  assert.equal(cardReadOnlyReason(personControl, unknown), '') // Edit is offered (it re-reads and re-checks)
  assert.equal(cardPermissionUnknown(personControl, unknown), PERMISSION_UNKNOWN_MESSAGE)
  assert.equal(cardSaveBlockedReason(personControl, unknown), PERMISSION_UNKNOWN_MESSAGE) // fail closed
  assert.doesNotMatch(PERMISSION_UNKNOWN_MESSAGE, /project admin decides/)
  // Not a person Control: the rule does not apply.
  assert.equal(cardPermissionUnknown({ access_id: 'agent-1', source: 'agent', properties: {} }, unknown), '')
})

test('the panel blocks Save on the save rule and shows the unknown notice', () => {
  assert.match(panel, /cardPermissionUnknown\(focusedCard, focusedViewer\) \? \(\s*<div className="notice" role="status">/)
  assert.match(panel, /const readOnlyReason = cardSaveBlockedReason\(item, focusedViewer\);/)
  assert.doesNotMatch(panel, /Boolean\(cardReadOnlyReason\(item, focusedViewer\)\)\}/)
})

// W587 follow-up C, the operator (2026-10-06 15:58): "It's clear that uh, something was restarted."
import {
  BOARD_RESTARTING_MESSAGE, isBoardRestarting, unavailableAccessCardMessage,
} from '../src/features/delegatedAccess/accessCardFocus.ts'
import { BOARD_RESTARTING_EDIT_MESSAGE } from '../src/features/delegatedAccess/cardEditability.ts'

test('a restarting board is said so: the viewer notice, still fail closed', () => {
  const restarting = { can_edit: null, reason: 'project_board_restarting', retryable: true }
  assert.equal(cardPermissionUnknown(personControl, restarting), BOARD_RESTARTING_EDIT_MESSAGE)
  assert.match(BOARD_RESTARTING_EDIT_MESSAGE, /^Problem Board is restarting; try again in a few seconds\./)
  assert.equal(cardSaveBlockedReason(personControl, restarting), BOARD_RESTARTING_EDIT_MESSAGE) // Save waits
  assert.equal(cardReadOnlyReason(personControl, restarting), '') // Edit stays
})

test('the restart codes are recognised exactly, and nothing else is', () => {
  assert.equal(isBoardRestarting('project_board_restarting'), true)
  assert.equal(isBoardRestarting('project_membership_provider_not_ready'), true) // an older package's 403
  assert.equal(isBoardRestarting('project_membership_provider_unavailable'), false)
  assert.equal(isBoardRestarting('project_person_control_decided_by_admin'), false)
  assert.equal(isBoardRestarting('xproject_board_restartingx'), false)
  assert.equal(isBoardRestarting(''), false)
})

test('a Card that did not open while the board restarted says so, not "does not exist"', () => {
  const focus = { accessId: 'person-control-1', controlOnly: true }
  const text = unavailableAccessCardMessage(focus, 'project_board_restarting')
  assert.equal(text, `Card person-control-1 was not loaded. ${BOARD_RESTARTING_MESSAGE}`)
  assert.doesNotMatch(text, /does not exist/)
  assert.match(unavailableAccessCardMessage(focus, 'other'), /does not exist or is not visible/)
})

test('the panel offers Try again on the restart and uses the restart text for Edit, Reload and Save', () => {
  assert.match(panel, /isBoardRestarting\(delegatedAccessError\) && controlFocus \? \(/)
  assert.match(panel, /onClick=\{\(\) => setFocusRetry\(\(n\) => n \+ 1\)\}>Try again<\/button>/)
  assert.match(panel, /\}, \[controlFocusValue, focusRetry, dispatch\]\);/)
  assert.match(panel, /lastReadError\.current = String\(error \|\| ''\);/)
  assert.match(panel, /isBoardRestarting\(lastReadError\.current\)\s*\? `\$\{BOARD_RESTARTING_MESSAGE\} The editor was not opened/)
  assert.match(panel, /isBoardRestarting\(lastReadError\.current\)\s*\? `\$\{BOARD_RESTARTING_MESSAGE\} Your draft is unchanged/)
  assert.equal((panel.match(/`Save was not applied: \$\{BOARD_RESTARTING_MESSAGE\} Your draft is kept\.`/g) || []).length, 2)
})
