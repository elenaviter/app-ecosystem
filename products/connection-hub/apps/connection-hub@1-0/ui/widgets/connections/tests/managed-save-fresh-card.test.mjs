// W661 save #2 (operator, 10 Oct: "i wont follow workaround, i want the fix"): after a managed person-Control
// save the server answers with the Card as it is now ("access"), so the open editor's next save starts from the
// saved revision and properties. Before the fix the managed answer carried no Card and the editor stayed on v12.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { freshestCard } from '../src/features/delegatedAccess/accessCardFocus.ts'
import { pinAfterSave } from '../src/features/delegatedAccess/cardFreshness.ts'

const slice = readFileSync(new URL('../src/features/delegatedAccess/delegatedAccessSlice.ts', import.meta.url), 'utf8')
const v12 = { access_id: 'person-control-1', source: 'control', card_revision: 12,
  properties: { 'connection_hub.control_snapshot': { basis_catalog_version: 'catalog-10-04' } } }
const v13 = { ...v12, card_revision: 13,
  properties: { 'connection_hub.control_snapshot': { basis_catalog_version: 'catalog-10-09' } } }

// The reducer step the slice runs on every save answer (updateDelegatedAccess.fulfilled), as asserted below.
const applied = (answer) => {
  let items = [v12]
  let focusedCard = v12
  if (answer.access) {
    items = items.map((item) => (item.access_id === answer.access.access_id ? answer.access : item))
    if (focusedCard.access_id === answer.access.access_id) focusedCard = answer.access
  }
  return freshestCard(items, focusedCard, v12.access_id)
}

test('the slice replaces the cached Card from a save answer\'s "access", committed or refused', () => {
  const fulfilled = slice.slice(slice.indexOf('.addCase(updateDelegatedAccess.fulfilled'),
    slice.indexOf('.addCase(updateDelegatedAccess.rejected'))
  assert.match(fulfilled, /if \(action\.payload\.access\) \{/)
  assert.match(fulfilled, /state\.items = state\.items\.map\(\(item\) => \(item\.access_id === updated\.access_id \? updated : item\)\)/)
  assert.match(fulfilled, /state\.focusedCard = updated/)
  assert.match(slice, /if \(res\?\.ok === false && res\?\.status === 409\) return res;/) // a 409 reaches the reducer
})

test('a managed save that answers with the Card moves the open editor to the saved revision and properties', () => {
  const record = applied({ ok: true, managed_card_edit: { state: 'committed', card_revision: 13 }, access: v13 })
  assert.equal(record.card_revision, 13)
  assert.deepEqual(record.properties, v13.properties) // the next save echoes v13, which the server compares equal
  assert.equal(pinAfterSave(12, v13), 13) // and expects revision 13: two saves in a row both go through
})

test('without the Card in the answer (the old managed reply) the editor stayed on v12: the defect', () => {
  assert.equal(applied({ ok: true, managed_card_edit: { state: 'committed', card_revision: 13 } }).card_revision, 12)
  assert.equal(pinAfterSave(12, undefined), 12)
})

test('a refused managed save that carries the current Card refreshes the record too', () => {
  assert.equal(applied({ ok: false, status: 409, error: 'managed_card_edit_fields_unsupported', access: v13 })
    .card_revision, 13)
})
