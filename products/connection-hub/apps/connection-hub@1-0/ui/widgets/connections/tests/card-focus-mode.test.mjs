import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { accessCardOpenMode } from '../src/features/delegatedAccess/cardFocusMode.ts'

const focus = (overrides = {}) => ({
  accessId: 'card-1',
  manualOnly: false,
  controlOnly: false,
  claims: [],
  ...overrides,
})

test('plain access and control Card links open in view mode', () => {
  assert.equal(accessCardOpenMode(focus()), 'view')
  assert.equal(accessCardOpenMode(focus({ controlOnly: true })), 'view')
  assert.equal(accessCardOpenMode(focus({ resource: 'service-only' })), 'view')
})

test('authority requests still open the Card editor', () => {
  assert.equal(accessCardOpenMode(focus({
    resource: 'service',
    outerOperation: 'documents.read',
  })), 'edit')
  assert.equal(accessCardOpenMode(focus({ claims: ['documents:read'] })), 'edit')
  assert.equal(accessCardOpenMode(focus({ accountClaim: 'mail:send' })), 'edit')
})

test('Cancel and Save return to the same focused Card view', () => {
  const panel = readFileSync(new URL(
    '../src/features/delegatedAccess/DelegatedAccessPanel.tsx',
    import.meta.url,
  ), 'utf8')
  const styles = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8')

  assert.match(panel, /accessCardOpenMode\(activeAccessCardFocus\) === 'edit'/)
  assert.match(panel, /const focusedViewRecord = !editingRecord && activeAccessCardFocus/)
  assert.match(panel, /const leaveFocusedCard = \(\) => \{\s*clearEditState\(\);\s*setAccessCardFocusDismissed\(true\);/)
  assert.doesNotMatch(panel, /const clearEditState = \(\) => \{[^}]*setAccessCardFocusDismissed/s)
  assert.match(panel, /if \(!isFocusedCard\(item\)\) setAccessCardFocusDismissed\(true\);\s*startEdit\(item\);/)
  assert.match(panel, /className="focused-card-view"/)
  assert.match(styles, /\.focused-card-view \{/)
})
