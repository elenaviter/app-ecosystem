// W727 (live W705, 11 Oct 13:46:33Z): after a committed Save of a project-managed Card, the screen's
// focused grant for each newly ticked operation must be made against the revision the previous write left.
// The managed Save answers with its transaction's committed revision, not a Card.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { agentGrantWirePayload } from '../src/features/delegatedAccess/agentGrantPayload.ts'
import { pinAfterCommit } from '../src/features/delegatedAccess/cardFreshness.ts'
import { focusedGrantArgs } from '../src/features/delegatedAccess/invocationChoice.ts'
import { resumeFor, runFocusedGrants } from '../src/features/delegatedAccess/focusedGrantRecovery.ts'

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

test('the panel builds every focused grant on the pin and runs them through the resumable orchestration', () => {
  const save = panel.slice(panel.indexOf('const saveEdit = async'), panel.indexOf('// The full delegable catalog'))
  assert.match(save, /resumeFor\(editResume\.current, item\.access_id, baseKey\)/)
  assert.match(save, /if \(!skipBase\) \{[^]*?dispatch\(updateDelegatedAccess\(/)
  assert.match(save, /runFocusedGrants\(\{[^]*?\{ expectedCardRevision: pin \?\? undefined \}[^]*?pin: editBaseRevision,[^]*?pinAfter: pinAfterCommit,/)
  assert.match(panel, /editResume\.current = null;[^]*?editResume\.current = null;/) // reset on Edit and on close
})

// CodeApp's W727 return (15:19Z): the panel's Retry Save after card_committed_policy_pending sent a new base Save
// on the old pin and a new change id. The real orchestration (runFocusedGrants + resumeFor) is driven here.
const partial = (revision) => ({ ok: false, status: 503, error: 'delegated_invocation_policy_unavailable',
  outcome: 'card_committed_policy_pending', access_id: 'person-my-card', access: { card_revision: revision } })
const ok = (revision) => ({ ok: true, managed_card_edit: { state: 'committed', card_revision: revision },
  access: { card_revision: revision } })

function editor() {
  const pin = { current: 26 }
  let resume = null
  let nonce = 0
  const sent = []
  const bases = []
  const save = async ({ baseKey, additions, answers }) => {
    const step = resumeFor(resume, 'person-my-card', baseKey)
    if (!step.skipBase) {
      bases.push(pin.current)
      pin.current = pinAfterCommit(pin.current, ok(pin.current + 1)) // the managed base Save's answer
    }
    const progress = { accessId: 'person-my-card', baseKey, completed: [...(step.resume?.completed || [])],
      pending: step.resume?.pending || null }
    resume = progress
    const outcome = await runFocusedGrants({
      progress, additions, pin, pinAfter: pinAfterCommit,
      build: ({ resource, operation, mode }, current) => focusedGrantArgs(
        { ...identity(operation), resource, changeId: `edit:${operation}:${++nonce}` }, mode, ['use'],
        { expectedCardRevision: current ?? undefined }),
      send: async (args) => {
        const wire = agentGrantWirePayload(args)
        sent.push(wire)
        const answer = answers.shift()
        if (answer instanceof Error) throw answer
        return answer
      },
    })
    if (outcome.state === 'done') resume = null
    return outcome
  }
  return { pin, sent, bases, save }
}

test('Retry Save after a policy-pending grant replays that exact grant and never resends the base Save', async () => {
  const e = editor()
  const additions = [{ resource: 'svc', operation: 'a', mode: 'always' }, { resource: 'svc', operation: 'b', mode: 'once' }]
  const first = await e.save({ baseKey: 'draft-1', additions, answers: [ok(28), partial(29)] })
  assert.equal(first.state, 'policy_pending')
  assert.equal(e.pin.current, 29) // the Card moved; the pin follows it
  assert.deepEqual(e.bases, [26])
  const pendingWire = e.sent[1]
  assert.equal(pendingWire.expected_card_revision, 28)
  // Retry Save. The refreshed Card already holds a and b, so the draft has no additions left.
  const retry = await e.save({ baseKey: 'draft-1', additions: [], answers: [ok(29)] })
  assert.equal(retry.state, 'done')
  assert.deepEqual(e.bases, [26]) // no second base Save
  assert.deepEqual(e.sent[2], pendingWire) // the same change id and revision: the project replays its decision
  assert.equal(e.pin.current, 29)
})

test('a retry before the Card is reloaded still sends only the missing grants after the exact replay', async () => {
  const e = editor()
  const additions = [{ resource: 'svc', operation: 'a', mode: 'always' }, { resource: 'svc', operation: 'b', mode: 'once' },
    { resource: 'svc', operation: 'c', mode: 'always' }]
  await e.save({ baseKey: 'draft-1', additions, answers: [ok(28), partial(29)] })
  const retry = await e.save({ baseKey: 'draft-1', additions, answers: [ok(29), ok(30)] })
  assert.equal(retry.state, 'done')
  assert.deepEqual(e.sent.map((wire) => wire.resource_operations.svc[0]), ['a', 'b', 'b', 'c'])
  assert.equal(e.sent[3].expected_card_revision, 29)
  assert.deepEqual(e.bases, [26])
})

test('a changed draft sends its base Save again, on the advanced pin, before the pending replay', async () => {
  const e = editor()
  const additions = [{ resource: 'svc', operation: 'b', mode: 'once' }]
  await e.save({ baseKey: 'draft-1', additions, answers: [partial(28)] })
  await e.save({ baseKey: 'draft-2', additions: [], answers: [ok(28)] })
  assert.deepEqual(e.bases, [26, 28])
})

test('a refused grant (nothing written) keeps the pin and resumes with the next Save, without a base Save', async () => {
  const e = editor()
  const additions = [{ resource: 'svc', operation: 'a', mode: 'always' }]
  const refused = await e.save({ baseKey: 'draft-1', additions, answers: [new Error('managed_card_edit_aborted')] })
  assert.equal(refused.state, 'refused')
  assert.equal(e.pin.current, 27)
  const retry = await e.save({ baseKey: 'draft-1', additions, answers: [ok(28)] })
  assert.equal(retry.state, 'done')
  assert.deepEqual(e.bases, [26])
  assert.equal(e.sent[1].expected_card_revision, 27)
})
