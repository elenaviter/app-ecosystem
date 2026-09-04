import assert from 'node:assert/strict'
import test from 'node:test'

import {
  driftNeedsReview,
  editedResourceKeys,
  offerReasonText,
  saveProblemText,
  saveProblems,
  toggleAccepted,
} from '../src/features/delegatedAccess/resourceEditing.ts'

const MEMORIES = 'https://host/api/mcp/memories*'
const TASKS = 'https://host/api/mcp/tasks*'
const MAIL = 'https://host/api/mcp/mail*'

test('an edit submits the card resources minus removed plus added, once each', () => {
  assert.deepEqual(editedResourceKeys([MEMORIES, TASKS], [MAIL], [TASKS]), [MEMORIES, MAIL])
  assert.deepEqual(editedResourceKeys([MEMORIES], [MEMORIES], []), [MEMORIES])
  assert.deepEqual(editedResourceKeys([MEMORIES], [TASKS], [TASKS]), [MEMORIES])
})

test('every unavailable offer says why in the grantor\'s words', () => {
  assert.equal(offerReasonText({ resource: TASKS, label: 'Tasks', identity_scope: 'grantor', compatible: true, reason: 'compatible' }), '')
  assert.equal(
    offerReasonText({ resource: MAIL, label: 'Mail', identity_scope: 'grantor_identity_family', compatible: false, reason: 'identity_scope_incompatible', card_identity_scope: 'grantor' }),
    'Runs under grantor_identity_family; this card acts as grantor, so it cannot be added.',
  )
  assert.equal(offerReasonText({ resource: '*', label: 'All', identity_scope: 'grantor', compatible: false, reason: 'admin_only' }), 'Only a platform administrator may delegate it.')
  assert.equal(offerReasonText({ resource: MEMORIES, label: 'Memories', identity_scope: 'grantor', compatible: false, reason: 'already_on_card' }), 'Already on this card.')
})

test('save is blocked when the card would be empty, an added resource has no claims, or a new operation has no choice', () => {
  const labelFor = (resource) => (resource === MAIL ? 'Mail' : 'Tasks')
  const empty = saveProblems({ resourceKeys: [], addedResources: [], claimsFor: () => [], missingChoices: [] })
  assert.deepEqual(empty, [{ code: 'no_resources_left' }])
  assert.equal(saveProblemText(empty[0], labelFor), 'Removing every resource revokes the card. Use Revoke instead.')

  const added = saveProblems({
    resourceKeys: [TASKS, MAIL],
    addedResources: [MAIL],
    claimsFor: (resource) => (resource === TASKS ? ['tasks:use'] : []),
    missingChoices: [{ resource: TASKS, operation: 'delete' }],
  })
  assert.deepEqual(added, [
    { code: 'added_resource_without_claims', resource: MAIL },
    { code: 'operation_without_choice', resource: TASKS, operations: ['delete'] },
  ])
  assert.equal(saveProblemText(added[0], labelFor), 'Select at least one access claim on Mail or remove it again.')
  assert.equal(saveProblemText(added[1], labelFor), 'Choose once or always for delete on Tasks.')

  const fine = saveProblems({ resourceKeys: [TASKS], addedResources: [], claimsFor: () => ['tasks:use'], missingChoices: [] })
  assert.deepEqual(fine, [])
})

test('accepting a changed descriptor is per resource and per operation', () => {
  let accepted = toggleAccepted({}, TASKS, 'delete', true)
  assert.deepEqual(accepted, { [TASKS]: ['delete'] })
  accepted = toggleAccepted(accepted, TASKS, 'search', true)
  assert.deepEqual(accepted, { [TASKS]: ['delete', 'search'] })
  accepted = toggleAccepted(accepted, MEMORIES, 'search', true)
  assert.deepEqual(accepted, { [TASKS]: ['delete', 'search'], [MEMORIES]: ['search'] })
  accepted = toggleAccepted(accepted, TASKS, 'delete', false)
  accepted = toggleAccepted(accepted, TASKS, 'search', false)
  assert.deepEqual(accepted, { [MEMORIES]: ['search'] })
})

test('the review renders only when the resource itself changed', () => {
  assert.equal(driftNeedsReview(undefined), false)
  assert.equal(driftNeedsReview({ status: 'current' }), false)
  assert.equal(driftNeedsReview({ status: 'unknown' }), false)
  assert.equal(driftNeedsReview({ status: 'changed', changed_operations: ['delete'] }), true)
  assert.equal(driftNeedsReview({ status: 'changed', added_operations: ['export'] }), true)
  assert.equal(driftNeedsReview({ status: 'removed' }), true)
})
