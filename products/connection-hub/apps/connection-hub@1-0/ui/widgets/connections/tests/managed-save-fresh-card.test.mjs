// W661 save #2 (operator, 10 Oct: "i wont follow workaround, i want the fix"): after a managed person-Control
// save the server answers with the Card as it is now ("access"), so the open editor's next save starts from the
// saved revision and properties. Before the fix the managed answer carried no Card and the editor stayed on v12.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { freshestCard } from '../src/features/delegatedAccess/accessCardFocus.ts'
import { isStaleEditRefusal, pinAfterSave } from '../src/features/delegatedAccess/cardFreshness.ts'
import {
  applicationOperationPropertiesForSelection, changedCardProperties, seedApplicationOperationRolePolicy,
} from '../src/features/delegatedAccess/applicationOperationRoles.ts'
import { cardConversationTargets, withConversationTargets } from '../src/features/delegatedAccess/conversationTargets.ts'

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

// Live 10 Oct 22:49Z: a Save from a fresh reload was refused in the server's properties pre-check. A managed
// person-Control Save carries the selection only; properties travel only when the person changed one.
const panel = readFileSync(new URL('../src/features/delegatedAccess/DelegatedAccessPanel.tsx', import.meta.url), 'utf8')
const v13Live = { // the live v13 Card's properties, as read (identifiers synthetic)
  'connection_hub.control_snapshot': { basis_catalog_version: 'delegated_catalog_2026-10-09', mode: 'exact',
    origin: 'reviewed', schema: 'connection_hub.control_snapshot.v1', state: 'exact' },
  'connection_hub.project_person_control': { project_ref: 'work:project:synthetic', project_subject: 'project-authority:s',
    schema: 'connection_hub.project_person_control.v1', target_subject: 'person-1' },
}
const built = (properties, policy, grants, selected = []) => withConversationTargets(applicationOperationPropertiesForSelection({
  properties, policy, selectedOperations: selected, resourceSelected: '*' in grants, resourcePreviouslySelected: '*' in grants,
}), cardConversationTargets(properties))

test('an unchanged Card sends no properties: the live Card (no application row) and a V1 policy Card', () => {
  const live = built(v13Live, seedApplicationOperationRolePolicy(v13Live, {}), {})
  assert.deepEqual(live, v13Live)
  assert.equal(changedCardProperties(live, built(v13Live, seedApplicationOperationRolePolicy(v13Live, {}), {})), undefined)

  const grants = { '*': ['kdcube:role:registered'] }
  const v1 = { ...v13Live, 'kdcube.application_operations': { schema: 'kdcube.application_operations.v1', mode: 'selected' } }
  const seeded = seedApplicationOperationRolePolicy(v1, grants)
  const sent = built(v1, seeded, grants, ['project.control.update'])
  assert.notDeepEqual(sent, v1) // the editor rewrites V1 as V2: echoed whole, the strict compare refuses it
  assert.equal(changedCardProperties(sent, built(v1, seedApplicationOperationRolePolicy(v1, grants), grants,
    ['project.control.update'])), undefined)
})

test('a changed property-backed choice still travels, and the server names that refusal', () => {
  const grants = { '*': ['kdcube:role:registered'] }
  const seeded = seedApplicationOperationRolePolicy(v13Live, grants)
  const changed = built(v13Live, { ...seeded, defaultRole: 'kdcube:role:paid' }, grants)
  assert.deepEqual(changedCardProperties(changed, built(v13Live, seeded, grants)), changed)
  assert.deepEqual(changedCardProperties({ a: [1, 2], b: { c: 1 } }, { b: { c: 1 }, a: [1, 2] }), undefined)
  assert.deepEqual(changedCardProperties({ a: [2, 1] }, { a: [1, 2] }), { a: [2, 1] })
})

test('the managed person-Control Save sends properties through changedCardProperties against the stored seed', () => {
  assert.match(panel, /properties: projectPersonControl\s*\? changedCardProperties\(selectedProperties, propertiesFor\(\s*seedApplicationOperationRolePolicy\(item\.properties, item\.resource_grants\),\s*cardConversationTargets\(item\.properties\),\s*\)\)\s*: selectedProperties,/)
  assert.match(panel, /const baseProperties = propertiesFor\(editApplicationRolePolicy, editConversationTargets\);/)
})

test('only a stale refusal reads "This access changed"; any other 409 shows the server\'s own message', () => {
  const fulfilled = slice.slice(slice.indexOf('.addCase(updateDelegatedAccess.fulfilled'),
    slice.indexOf('.addCase(updateDelegatedAccess.rejected'))
  assert.match(fulfilled, /state\.error = isStaleEditRefusal\(action\.payload\)\s*\? 'This access changed while you were editing it\.[^']*'\s*: action\.payload\.message \|\|/)
  assert.equal(isStaleEditRefusal({ status: 409, error: 'managed_card_edit_fields_unsupported' }), false)
  assert.equal(isStaleEditRefusal({ status: 409, error: 'delegated_card_revision_conflict' }), true)
})
