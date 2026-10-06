// W587, the operator's rule (2026-10-06 14:50): "i asked not to refetch! i asked only when card is
// opened in connection hub (clicked on it for preview) and when edit is pressed. only then".
// The 14:48 rollback: an [items] effect re-read an open off-list Card, the read replaced the list
// with a new array, and the effect fired again (119 project_person_control_get in 5 minutes).
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const read = (path) => readFileSync(new URL(`../src/features/delegatedAccess/${path}`, import.meta.url), 'utf8')
const panel = read('DelegatedAccessPanel.tsx')
const slice = read('delegatedAccessSlice.ts')

test('the Card is read only on open, on Edit and on the explicit Reload this Card', () => {
  const calls = panel.match(/void readCurrentCard\(|await readCurrentCard\(/g) || []
  // open (the view effect), Edit (beginEdit), Reload this Card (reloadEdit): nothing else.
  assert.equal(calls.length, 3, `read call sites: ${calls.length}`)
  assert.match(panel, /const current = await readCurrentCard\(item\);\s+\/\/ A later Edit/)
  assert.match(panel, /const reloadEdit = async[^]*?const current = await readCurrentCard\(item\);/)
  assert.match(panel, /if \(record\) void readCurrentCard\(record\);\s+\/\/ eslint-disable-next-line react-hooks\/exhaustive-deps\s+\}, \[viewAccessId\]\);/)
})

test('nothing re-reads while a Card is open: no tab-return, list-change or post-409 read', () => {
  assert.doesNotMatch(panel, /visibilitychange/)
  assert.doesNotMatch(panel, /\}, \[items\]\);/)
  assert.doesNotMatch(panel, /setEditRefusedStale\(true\);\s+void readCurrentCard/)
  // The open read is keyed on the opened Card only, so a re-render never repeats it.
  const openEffect = panel.slice(panel.indexOf('// W587, the operator\'s rule (14:50)'), panel.indexOf('}, [viewAccessId]);'))
  assert.doesNotMatch(openEffect, /\[items|\[focusedCard|readCurrentCard\]/)
})

test('a Control link is read once to open it; the view does not read it a second time', () => {
  assert.match(panel, /if \(found\) \{\s+openReadDone\.current = accessCardFocus\.accessId;/)
  assert.match(panel, /if \(openReadDone\.current === viewAccessId\) \{\s+openReadDone\.current = null;\s+return;/)
  assert.match(panel, /if \(!viewAccessId\) \{\s+openReadDone\.current = null;\s+return;/)
})

test('busy clears when the last Control read settles, never sticks after a superseded one', () => {
  assert.match(slice, /loadControlCard\.pending, \(state\) => \{\s+state\.controlReads \+= 1;\s+state\.busy = true;/)
  const settled = slice.match(/state\.controlReads = Math\.max\(0, state\.controlReads - 1\);\s+if \(state\.controlReads === 0\) state\.busy = false;/g) || []
  assert.equal(settled.length, 2) // fulfilled and rejected
})
