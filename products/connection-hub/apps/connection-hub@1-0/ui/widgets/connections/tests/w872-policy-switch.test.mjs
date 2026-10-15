// W872. The operator, 2026-10-15 ~18:47Z: "when i click through the card during edit THIS MUST NOT CAUSE NO ANY
// CALLS! i must simply be able normally pick what i need! only when i press save someting can be asked".
// The Every time / Once switch of an operation the Card already holds changes only the edit draft; a changed mode
// enables Save and is listed; Save sends it with everything else; Cancel drops it.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { heldModeChanges, policySwitchView, withInvocationPolicy } from '../src/features/delegatedAccess/invocationChoice.ts'

const read = (path) => readFileSync(new URL(`../src/features/delegatedAccess/${path}`, import.meta.url), 'utf8')

test('a click shows the chosen mode at once from the draft, with no request', () => {
  const held = { mode: 'always' }
  assert.deepEqual(policySwitchView(held, undefined, false),
    { shown: 'always', changed: false, alwaysDisabled: true, onceDisabled: false, used: false })
  assert.deepEqual(policySwitchView(held, 'once', false),
    { shown: 'once', changed: true, alwaysDisabled: false, onceDisabled: true, used: false })
  // Switching back to the stored mode is no change.
  assert.equal(policySwitchView(held, 'always', false).changed, false)
  // No stored policy runs every time.
  assert.equal(policySwitchView(undefined, undefined, false).shown, 'always')
  // A used Once is re-armed by choosing Once again, and that is a change.
  const used = { mode: 'once', remaining: 0 }
  assert.equal(policySwitchView(used, undefined, false).onceDisabled, false)
  assert.equal(policySwitchView(used, undefined, false).used, true)
  assert.equal(policySwitchView(used, 'once', false).changed, true)
  // The control's click only calls onSet; it holds no request of its own.
  const controls = read('InvocationControls.tsx')
  const control = controls.slice(controls.indexOf('export function InvocationPolicyControl'),
    controls.indexOf('export function OperationInvocationChoice'))
  assert.match(control, /onClick=\{\(\) => onSet\('always'\)\}/)
  assert.match(control, /onClick=\{\(\) => onSet\('once'\)\}/)
  assert.doesNotMatch(control, /dispatch|postOp|fetch|InvocationPolicy\(/)
  assert.match(controls, /Changes when you press Save\./)
})

test('the held operations whose mode the draft changed are what Save sends', () => {
  const policies = { 'svc:a': { mode: 'always' }, 'svc:b': { mode: 'once', remaining: 1 }, 'svc:c': { mode: 'once', remaining: 0 } }
  const policyOf = (resource, operation) => policies[`${resource}:${operation}`]
  const held = { svc: ['a', 'b', 'c', 'd'] }
  assert.deepEqual(heldModeChanges(policyOf, held, {}), [])
  assert.deepEqual(heldModeChanges(policyOf, held, { 'svc:a': 'once', 'svc:b': 'once', 'svc:c': 'once', 'svc:d': 'always' }), [
    { resource: 'svc', operation: 'a', from: 'always', to: 'once' },
    { resource: 'svc', operation: 'c', from: 'once', to: 'once' },
  ])
  // An operation not held (an addition) is not a held change; its choice travels as an addition.
  assert.deepEqual(heldModeChanges(policyOf, { svc: ['a'] }, { 'svc:x': 'once' }), [])
})

test('the panel makes no policy call, sends held changes with Save, lists them, and Cancel drops them', () => {
  const panel = read('DelegatedAccessPanel.tsx')
  assert.doesNotMatch(panel, /setDelegatedInvocationPolicy|setOperationInvocationPolicy/)
  assert.match(panel, /draftMode=\{editInvocationModes\[`\$\{resource\}:\$\{operation\.name\}`\]\}/)
  assert.match(panel, /onSet=\{\(mode\) => setEditInvocationModes\(\(current\) => \(\{/)
  const save = panel.slice(panel.indexOf('const saveEdit = async'))
  assert.match(save.slice(0, save.indexOf('const savedResourceOperations')),
    /editHeldModeChanges\(item\)\.forEach\(\(\{ resource, operation, to \}\) => \{\n\s*invocationModes\[resource\] =/)
  assert.match(panel, /Changes when you press Save:\{' '\}/)
  const clear = panel.slice(panel.indexOf('const clearEditState = () =>'))
  assert.match(clear.slice(0, 1500), /setEditInvocationModes\(\{\}\);/)
})

test('a written policy still updates both copies of the Card', () => {
  const policy = { policy_id: 'p1', mode: 'once', authority: { access_id: 'aut_1' } }
  const card = { access_id: 'aut_1', card_revision: 12, invocation_policies: [{ ...policy, mode: 'always' }] }
  assert.deepEqual(withInvocationPolicy(card, policy).invocation_policies, [policy])
})
