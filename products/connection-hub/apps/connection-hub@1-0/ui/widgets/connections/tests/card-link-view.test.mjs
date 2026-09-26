import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  accessCardFocusFromParams,
  accessCardFocusRequestsEdit,
} from '../src/features/delegatedAccess/accessCardFocus.ts'

// W304 finding 21: a link to a Card opened it straight in the editor, and
// Cancel or Save left the person on the full list with the Card gone. A link
// now opens the Card to read, on its own; only a link that asks for a change
// opens the editor; Save and Cancel come back to the same Card.

const focus = (values) => accessCardFocusFromParams((key) => String(values[key] || ''))

test('a plain Card link opens it to read, a request-bound link opens the editor', () => {
  assert.equal(accessCardFocusRequestsEdit(focus({ access_id: 'card-one' })), false)
  assert.equal(accessCardFocusRequestsEdit(focus({ access_id: 'card-one', resource: 'problem-board' })), false)
  assert.equal(accessCardFocusRequestsEdit(focus({ access_id: 'card-one', resource: 'problem-board', outer_operation: 'work.report' })), true)
  assert.equal(accessCardFocusRequestsEdit(focus({ access_id: 'card-one', account_claim: 'gmail:read' })), true)
  assert.equal(accessCardFocusRequestsEdit(focus({ access_id: 'card-one', claims: 'docs:read' })), true)
})

const panel = readFileSync(new URL('../src/features/delegatedAccess/DelegatedAccessPanel.tsx', import.meta.url), 'utf8')

test('the linked Card is shown on its own, read only, until Edit', () => {
  assert.match(panel, /setViewAccessId\(item\.access_id\);\s+\/\/ A link opens the Card to read\.[^]*?if \(accessCardFocusRequestsEdit\(accessCardFocus\)\) startEdit\(item\);/)
  // The old unconditional edit on every resolved link is gone.
  assert.doesNotMatch(panel, /focusedAccessId\.current = accessCardFocus\.accessId;\s+startEdit\(item\);/)
  assert.match(panel, /\{!editingRecord && viewRecord \? \(/)
  assert.match(panel, /aria-label="Linked card"/)
  assert.match(panel, /renderDetailedAgentCard\(viewRecord\)\s+: renderDetailedOtherCard\(viewRecord\)/)
  // The full list stays hidden while the linked Card is shown.
  assert.match(panel, /\{!editingRecord && !viewRecord && compactList \? renderCompactList\(\) : null\}/)
  assert.match(panel, /\{!editingRecord && !viewRecord && !compactList \? \(/)
})

test('Save and Cancel return to the linked Card; All cards leaves it', () => {
  // clearEditState (Save, Cancel) never clears the linked Card, so the view
  // above renders again; only the All cards buttons and a new link do.
  const clear = panel.slice(panel.indexOf('const clearEditState = () => {'), panel.indexOf('};', panel.indexOf('const clearEditState = () => {')))
  assert.doesNotMatch(clear, /setViewAccessId/)
  assert.match(panel, /onClick=\{\(\) => \{\s+setViewAccessId\(null\);\s+if \(editDirty\) setPendingLeave\(\{ kind: 'leave' \}\);\s+else clearEditState\(\);/)
  assert.match(panel, /onClick=\{\(\) => setViewAccessId\(null\)\}>\s+All cards/)
})
