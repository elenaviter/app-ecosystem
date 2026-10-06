// W587, the operator's rule: a Card is read on open and on Edit, and an edit the
// server version has overtaken cannot be saved until the Card is reloaded.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { accessCardFocusFromParams, freshestCard, staleEdit } from '../src/features/delegatedAccess/accessCardFocus.ts'
import {
  controlFocusRead,
  pinAfterRefusal,
  pinAfterSave,
  pinAtStart,
  catalogPinAtStart,
  withLinkedOperation,
  withNewerCard,
} from '../src/features/delegatedAccess/cardFreshness.ts'

const panel = readFileSync(
  new URL('../src/features/delegatedAccess/DelegatedAccessPanel.tsx', import.meta.url),
  'utf8',
)
const focus = (values) => accessCardFocusFromParams((key) => values[key] || '')
const r4 = { access_id: 'control-1', card_revision: 4, catalog_version: 'catalog-10-03' }
const r5 = { access_id: 'control-1', card_revision: 5, catalog_version: 'catalog-10-04' }
const r6 = { access_id: 'control-1', card_revision: 6, catalog_version: 'catalog-10-04' }

test('the operator sequence: an edit on r4 overtaken by r5 cannot be saved until the Card is reloaded', () => {
  let pin = pinAtStart(r4) // Edit pressed while the server held r4
  let refused = false
  assert.equal(staleEdit(pin, r4.card_revision, refused), false)
  // Another admin saves r5; a refresh (tab return, Refresh) shows it. The pin does not move.
  assert.equal(staleEdit(pin, r5.card_revision, refused), true)
  // Even if the page had not noticed, the server refuses the save: 409.
  pin = pinAfterRefusal(pin)
  refused = true
  assert.equal(pin, 4)
  // A later refresh to r6 between the 409 and the next click changes nothing: still refused, still pinned to 4.
  assert.equal(staleEdit(pin, r6.card_revision, refused), true)
  assert.equal(pinAfterRefusal(pin), 4)
  // Only "Reload this Card" (a new start on the server version) makes the edit savable again.
  pin = pinAtStart(r6)
  refused = false
  assert.equal(staleEdit(pin, r6.card_revision, refused), false)
})

test('a successful save moves the pin to the revision the server wrote, never back', () => {
  assert.equal(pinAfterSave(4, { card_revision: 5 }), 5)
  assert.equal(pinAfterSave(4, undefined), 4)
  assert.equal(pinAfterSave(4, {}), 4)
  assert.equal(pinAtStart({}), null)
})

test('a Control link always reads the server, whatever the tab has cached', () => {
  const link = focus({ control_card_id: 'control-1', project_ref: 'work:project:maintenance' })
  assert.deepEqual(controlFocusRead(link), {
    controlId: 'control-1', projectRef: 'work:project:maintenance', targetSubject: undefined, invitationRef: undefined,
  })
  assert.equal(controlFocusRead(focus({ access_id: 'agent-1' })), null) // not a Control link
  assert.equal(controlFocusRead(null), null)
})

test('a newer read replaces the list row; an older, equal or foreign one returns the SAME array', () => {
  const other = { access_id: 'agent-2', card_revision: 9 }
  const list = [r4, other]
  const replaced = withNewerCard(list, r5)
  assert.deepEqual(replaced, [r5, other])
  assert.notEqual(replaced, list)
  // W587 loop: identity must not change when nothing is newer, or anything keyed on the list fires again.
  const current = [r5, other]
  assert.equal(withNewerCard(current, r4), current)
  assert.equal(withNewerCard(current, r5), current)
  assert.equal(withNewerCard(current, { access_id: 'unknown', card_revision: 99 }), current)
  assert.equal(withNewerCard(current, null), current)
  assert.equal(freshestCard(withNewerCard([r4], r5), r5, 'control-1'), r5)
})

test('the panel moves the pin only through these rules', () => {
  const assignments = panel.match(/editBaseRevision\.current = [^;]+;/g)
  assert.deepEqual(assignments, [
    'editBaseRevision.current = pinAtStart(item);',
    'editBaseRevision.current = null;', // clearEditState: the editor closed
    'editBaseRevision.current = pinAfterRefusal(editBaseRevision.current);',
    'editBaseRevision.current = pinAfterSave(editBaseRevision.current, updated.access);',
  ])
})

test('the focus effect reads through the rule, with no cached-copy shortcut', () => {
  const focusEffect = panel.slice(panel.indexOf('const read = controlFocusRead(accessCardFocus);'), panel.indexOf('}, [controlFocusValue, focusRetry, dispatch]);'))
  assert.match(focusEffect, /if \(!read \|\| !accessCardFocus\) return;/)
  assert.match(focusEffect, /dispatch\(loadControlCard\(read\)\)/)
  assert.doesNotMatch(focusEffect, /focusedCard|findAccessCardFocus|items\./)
})

test('a Control or project agent Card read updates the list row in the store', () => {
  const slice = readFileSync(new URL('../src/features/delegatedAccess/delegatedAccessSlice.ts', import.meta.url), 'utf8')
  assert.match(slice, /loadProjectAgentCard\.fulfilled[^]*?state\.items = withNewerCard\(state\.items, action\.payload\);/)
  assert.match(slice, /loadControlCard\.fulfilled[^]*?state\.items = withNewerCard\(state\.items, action\.payload\.access\);/)
})

test('an edit link keeps its requested operation: it is added after the read seeded the draft', () => {
  const seeded = { '*/mcp/problem_board*': ['plan.item.update'] } // what startEdit seeded from the server read
  assert.deepEqual(withLinkedOperation(seeded, '*/mcp/problem_board*', 'project.role.assign'), {
    '*/mcp/problem_board*': ['plan.item.update', 'project.role.assign'],
  })
  assert.deepEqual(withLinkedOperation(seeded, '*/mcp/problem_board*', 'plan.item.update'), seeded)
  assert.deepEqual(withLinkedOperation({}, 'r', 'op'), { r: ['op'] })
  assert.equal(withLinkedOperation(seeded, undefined, 'op'), seeded)
  // In the panel the addition runs in beginEdit's continuation, never before it (EMain and Ops B1, 13:49).
  const link = panel.slice(panel.indexOf('if (accessCardFocusRequestsEdit(accessCardFocus)) {'), panel.indexOf('if (accessCardFocus.accountId) {'))
  assert.match(link, /void beginEdit\(item\)\.then\(\(opened\) => \{\s+if \(opened\) setEditResourceOperations\(\(current\) => withLinkedOperation\(current, resource, outerOperation\)\);/)
  assert.equal((link.match(/setEditResourceOperations/g) || []).length, 1)
})

test('a later Edit supersedes an earlier one whose read is still in flight', () => {
  const begin = panel.slice(panel.indexOf('const beginEdit = useCallback('), panel.indexOf('}, [readCurrentCard, startEdit]);'))
  assert.match(begin, /editRequest\.current = item\.access_id;\s+const current = await readCurrentCard\(item\);\s+[^]*?if \(editRequest\.current !== item\.access_id\) return false;[^]*?startEdit\(current\);\s+return true;/)
})

test('the catalog is pinned with the revision when the edit starts', () => {
  assert.equal(catalogPinAtStart({ catalog_version: 'c-10-03' }), 'c-10-03')
  assert.equal(catalogPinAtStart({ catalog_version: 'c-10-03', catalog_drift: { current_version: 'c-10-04' } }), 'c-10-04')
  assert.equal(catalogPinAtStart({}), null)
  assert.match(panel, /editBaseCatalog\.current = catalogPinAtStart\(item\);/)
  assert.match(panel, /expectedCatalogVersion: editBaseCatalog\.current \?\? catalogPinAtStart\(item\) \?\? undefined,/)
})
