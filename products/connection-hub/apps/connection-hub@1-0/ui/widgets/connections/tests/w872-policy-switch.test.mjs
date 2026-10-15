// W872 (operator, 2026-10-15 18:28Z): "i first thought that "once/always" is not switching and is not clickable to
// switch in the existing selected options - that long that took to switch. this is awful. but actually changing
// once/always on selected option does not count as a difference and when i attempt to save this change (same action
// seletced bit different mode) this does not work and it say no changes."
// The switch on a held operation commits on click (delegated_invocation_policy_set); EMain 18:34Z: click-commit, no
// server change. It must show the chosen mode at once, update the Card the editor shows, and show a refusal.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { policySwitchView, withInvocationPolicy } from '../src/features/delegatedAccess/invocationChoice.ts'

const read = (path) => readFileSync(new URL(`../src/features/delegatedAccess/${path}`, import.meta.url), 'utf8')

test('a click shows the chosen mode at once, before Connection Hub answers, and both buttons wait', () => {
  const held = { mode: 'always', remaining: undefined }
  assert.deepEqual(policySwitchView(held, undefined, false),
    { shown: 'always', saving: false, alwaysDisabled: true, onceDisabled: false, used: false })
  // The same frame as the click: the pending mode is what the switch shows.
  assert.deepEqual(policySwitchView(held, 'once', false),
    { shown: 'once', saving: true, alwaysDisabled: true, onceDisabled: true, used: false })
  // And back: Once to Every time is offered and shown at once too.
  const once = { mode: 'once', remaining: 1 }
  assert.equal(policySwitchView(once, undefined, false).alwaysDisabled, false)
  assert.equal(policySwitchView(once, 'always', false).shown, 'always')
  // A used Once stays re-armable.
  assert.equal(policySwitchView({ mode: 'once', remaining: 0 }, undefined, false).onceDisabled, false)
  assert.equal(policySwitchView({ mode: 'once', remaining: 0 }, undefined, false).used, true)
})

test('a written policy updates the focused Card the editor shows, not only the listed one', () => {
  const policy = { policy_id: 'p1', mode: 'once', revision: 4, authority: { access_id: 'aut_1', operation: 'note.view.fail' } }
  const old = { ...policy, mode: 'always', revision: 3 }
  const card = { access_id: 'aut_1', card_revision: 12, invocation_policies: [old] }
  const updated = withInvocationPolicy(card, policy)
  assert.deepEqual(updated.invocation_policies, [policy])
  assert.equal(updated.card_revision, 12, 'a policy write does not move card_revision')
  assert.equal(withInvocationPolicy({ access_id: 'other' }, policy).invocation_policies, undefined)
  const slice = read('delegatedAccessSlice.ts')
  const fulfilled = slice.slice(slice.indexOf('setDelegatedInvocationPolicy.fulfilled'))
  assert.match(fulfilled.slice(0, 900), /state\.items = state\.items\.map\(withPolicy\)/)
  assert.match(fulfilled.slice(0, 900), /if \(state\.focusedCard\) state\.focusedCard = withPolicy\(state\.focusedCard\)/)
})

test('the panel sets the pending mode before the write, shows a refusal, and passes the pending mode', () => {
  const panel = read('DelegatedAccessPanel.tsx')
  const handler = panel.slice(panel.indexOf('const setOperationInvocationPolicy'))
  const body = handler.slice(0, handler.indexOf('\n  };'))
  assert.ok(body.indexOf('setPendingPolicyModes') < body.indexOf('dispatch(setDelegatedInvocationPolicy'))
  assert.doesNotMatch(body, /\.catch\(\(\) => undefined\)/)
  assert.match(body, /setEditActionError\(`\$\{operation\} was not switched to/)
  assert.match(panel, /pendingMode=\{pendingPolicyModes\[policyModeKey\(item\.access_id, resource, operation\.name\)\]\}/)
})

test('the switch says a click saves, and the mode not in effect looks clickable', () => {
  const controls = read('InvocationControls.tsx')
  assert.match(controls, /Saved as soon as you click; no Save needed\./)
  assert.match(controls, /saving…/)
  const css = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8')
  assert.match(css, /\.invocation-policy-control button:not\(:disabled\):not\(\.active\) \{[^}]*text-decoration: underline dotted/)
})
