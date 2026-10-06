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
