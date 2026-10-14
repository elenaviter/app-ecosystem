// W727 (live W705, 11 Oct 13:46:33Z): after a committed Save of a project-managed Card, the screen's
// focused grant for each newly ticked operation must be made against the revision the previous write left.
// The managed Save answers with its transaction's committed revision, not a Card.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { agentGrantWirePayload } from '../src/features/delegatedAccess/agentGrantPayload.ts'
import { pinAfterCommit } from '../src/features/delegatedAccess/cardFreshness.ts'
import { focusedGrantArgs } from '../src/features/delegatedAccess/invocationChoice.ts'
import { isPolicyPending } from '../src/features/delegatedAccess/focusedGrantRecovery.ts'

const panel = readFileSync(
  new URL('../src/features/delegatedAccess/DelegatedAccessPanel.tsx', import.meta.url),
  'utf8',
)
const identity = (operation) => ({
  clientId: 'kdcube-agent:pb:me', accessId: 'person-my-card', resource: 'svc', operation, changeId: `edit:${operation}`,
})
const committed = (revision) => ({ ok: true, managed_card_edit: { state: 'committed', card_revision: revision } })

test('the live sequence: a managed base Save, then two added operations, each on the advanced revision', () => {
  let pin = 26 // Edit opened on r26
  pin = pinAfterCommit(pin, committed(27)) // the base Save: the project's transaction wrote r27, no Card returned
  const first = agentGrantWirePayload(focusedGrantArgs(identity('a'), 'always', ['use'], { expectedCardRevision: pin }))
  assert.equal(first.expected_card_revision, 27)
  pin = pinAfterCommit(pin, { ...committed(28), access: { card_revision: 28 } })
  const second = agentGrantWirePayload(focusedGrantArgs(identity('b'), 'once', ['use'], { expectedCardRevision: pin }))
  assert.equal(second.expected_card_revision, 28)
  assert.deepEqual(second.resource_operations, { svc: ['b'] })
})

test('only a committed answer moves the pin', () => {
  assert.equal(pinAfterCommit(5, { ok: false, managed_card_edit: { state: 'aborted', card_revision: 6 } }), 5)
  assert.equal(pinAfterCommit(5, { ok: false, managed_card_edit: { state: 'pending', card_revision: 6 } }), 5)
  assert.equal(pinAfterCommit(5, undefined), 5)
  assert.equal(pinAfterCommit(5, { access: { card_revision: 6 } }), 6) // an ordinary Card's save
})

test('a grant without a pinned revision sends exactly the old payload', () => {
  const before = agentGrantWirePayload(focusedGrantArgs(identity('a'), 'always', ['use']))
  const unpinned = agentGrantWirePayload(focusedGrantArgs(identity('a'), 'always', ['use'], { expectedCardRevision: undefined }))
  assert.deepEqual(unpinned, before)
  assert.ok(!('expected_card_revision' in before))
})

// W755 (operator, 2026-10-11: "WHY ITS NOT A PART"): the Save no longer sends focused grants. It sends the whole
// Card once with the policy of every operation it adds, and the Card and its policies commit together.
test('a Save is one request: the whole Card with each addition\'s policy, no focused grants', () => {
  const save = panel.slice(panel.indexOf('const saveEdit = async'), panel.indexOf('// The full delegable catalog'))
  assert.equal((save.match(/updateDelegatedAccess\(\{/g) || []).length, 1)
  assert.doesNotMatch(save, /runFocusedGrants|resumeFor|grantAgentAccess/)
  assert.match(save, /invocationModes,/)
  assert.doesNotMatch(save, /isPolicyPending/)
})

// The agent-request approval flow still recognises a policy-pending grant answer.
test('a policy-pending answer is recognised; anything else is not', () => {
  assert.equal(isPolicyPending({ ok: false, outcome: 'card_committed_policy_pending' }), true)
  assert.equal(isPolicyPending({ ok: false, error: 'x' }), false)
  assert.equal(isPolicyPending({ ok: true, outcome: 'card_committed_policy_pending' }), false)
  assert.equal(isPolicyPending(null), false)
})
