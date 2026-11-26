// An Agent Card's Save keeps what was granted, so older conversations keep
// their choices; "Save and revoke unticked" also withdraws every permission
// left unticked, everywhere, after a confirmation.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const panel = readFileSync(
  new URL('../src/features/delegatedAccess/DelegatedAccessPanel.tsx', import.meta.url), 'utf8')
const slice = readFileSync(
  new URL('../src/features/delegatedAccess/delegatedAccessSlice.ts', import.meta.url), 'utf8')

test('the revoke button is offered only on an Agent Card and asks first', () => {
  const button = panel.indexOf('Save and revoke unticked')
  assert.ok(button > 0, 'the button is rendered')
  const before = panel.slice(Math.max(0, button - 900), button)
  assert.match(before, /\{residentCapabilityCard \? \(/)
  assert.match(before, /onClick=\{\(\) => setConfirmRevokeUnselected\(record\)\}/)
  assert.match(panel, /\{renderRevokeUnselectedDialog\(\)\}/)
})

test('confirming saves once with revoke; Save alone never revokes', () => {
  assert.match(panel, /if \(pending\) void saveEdit\(pending, true\);/)
  assert.match(panel, /onClick=\{\(\) => saveEdit\(record\)\}/)
  assert.match(panel, /revokeUnselected: revokeUnselected \|\| undefined,/)
  assert.match(slice, /\.\.\.\(revokeUnselected \? \{ revoke: true \} : \{\}\),/)
})

test('the dialog says older conversations lose the unticked permissions', () => {
  const start = panel.indexOf('const renderRevokeUnselectedDialog = () => (')
  const dialog = panel.slice(start, panel.indexOf('\n  );\n', start))
  assert.match(dialog, /every conversation, older ones included, loses it at once/)
  assert.match(dialog, /confirmLabel="Save and revoke"/)
})
