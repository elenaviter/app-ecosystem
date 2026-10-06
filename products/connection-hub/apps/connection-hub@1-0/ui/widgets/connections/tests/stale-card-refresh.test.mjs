// W587: a Card shown or edited is the current server revision, never an older cached copy.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  accessCardFocusFromParams,
  controlFocusKey,
  freshestCard,
  staleEdit,
} from '../src/features/delegatedAccess/accessCardFocus.ts'

const panel = readFileSync(
  new URL('../src/features/delegatedAccess/DelegatedAccessPanel.tsx', import.meta.url),
  'utf8',
)

test('the owner list read at page open never hides a newer Control read later', () => {
  // The operator's case: the list holds r4, another admin saved r5, the focused read returned r5.
  const listed = { access_id: 'control-1', card_revision: 4, catalog_version: 'catalog-10-03' }
  const read = { access_id: 'control-1', card_revision: 5, catalog_version: 'catalog-10-04' }
  assert.equal(freshestCard([listed], read, 'control-1'), read)
  // And the other way round: a newer list entry beats an older focused copy.
  assert.equal(freshestCard([read], listed, 'control-1'), read)
  // A tie keeps the focused read, the later one.
  const sameRevision = { ...listed, card_revision: 5 }
  assert.equal(freshestCard([sameRevision], read, 'control-1'), read)
})

test('freshestCard answers only for the Card asked for', () => {
  const other = { access_id: 'control-2', card_revision: 9 }
  assert.equal(freshestCard([other], null, 'control-1'), undefined)
  assert.equal(freshestCard([], other, 'control-1'), undefined)
  assert.equal(freshestCard([other], undefined, 'control-2'), other)
  assert.equal(freshestCard([other], other, null), undefined)
})

test('a Control focus key differs when the project is switched in place', () => {
  const focus = (projectRef) => accessCardFocusFromParams((key) => ({
    control_card_id: 'control-1', project_ref: projectRef,
  })[key] || '')
  assert.notEqual(controlFocusKey(focus('work:project:kickstart')), controlFocusKey(focus('work:project:maintenance')))
  assert.equal(controlFocusKey(focus('work:project:maintenance')), controlFocusKey(focus('work:project:maintenance')))
})

test('a Control link always reads the server, even when a copy is cached', () => {
  const effect = panel.slice(panel.indexOf('const [controlFocusReadKey'), panel.indexOf("document.addEventListener('visibilitychange'"))
  assert.match(effect, /dispatch\(loadControlCard\(/)
  assert.doesNotMatch(effect, /if \(focusedCard && matchesAccessCardFocus\(focusedCard, accessCardFocus\)\)/)
  assert.match(panel, /accessCardFocus\.controlOnly && controlFocusReadKey !== controlFocusKey\(accessCardFocus\)\) return;/)
})

test('views and edits use the freshest copy, not the list entry first', () => {
  assert.match(panel, /const viewRecord = viewAccessId\s*\?\s*freshestCard\(items, focusedCard, viewAccessId\)/)
  assert.match(panel, /const editingRecord = editingAccessId\s*\?\s*freshestCard\(items, focusedCard, editingAccessId\)/)
  assert.doesNotMatch(panel, /items\.find\(\(item\) => item\.access_id === viewAccessId\)/)
})

test('an edit is stale once the server is ahead of its pin or refused it; only a reload clears it', () => {
  assert.equal(staleEdit(4, 4, false), false)
  assert.equal(staleEdit(4, 5, false), true) // another admin saved r5 while this edit was on r4
  assert.equal(staleEdit(5, 5, true), true) // the server refused with 409 (card or catalog moved)
  assert.equal(staleEdit(null, 9, false), false) // no open edit
})

test('Edit reads the Card from the server before it seeds and pins the draft', () => {
  const begin = panel.slice(panel.indexOf('const beginEdit = useCallback('), panel.indexOf('}, [readCurrentCard, startEdit]);'))
  assert.match(begin, /const current = await readCurrentCard\(item\);[^]*?startEdit\(current\);/)
  assert.match(begin, /if \(!current\) \{[^]*?return;/) // no read, no editor
  // Every way into an edit goes through it.
  assert.match(panel, /onClick=\{\(\) => \{ void beginEdit\(item\); \}\}>/)
  assert.match(panel, /if \(!editDirty\) \{ void beginEdit\(item\); return; \}/)
  assert.match(panel, /if \(action\.kind === 'switch'\) void beginEdit\(action\.item\);/)
  assert.doesNotMatch(panel, /onClick=\{\(\) => startEdit\(item\)\}/)
  // The read covers every kind of Card.
  const read = panel.slice(panel.indexOf('const readCurrentCard = useCallback('), panel.indexOf('const beginEdit = useCallback('))
  assert.match(read, /dispatch\(loadControlCard\(/)
  assert.match(read, /dispatch\(loadProjectAgentCard\(projectAgent\)\)/)
  assert.match(read, /dispatch\(loadDelegatedAccess\(\)\)/)
})

test('after a 409 a second Save sends nothing and the pin is unchanged', () => {
  const save = panel.slice(panel.indexOf('const saveEdit = async (item: DelegatedAccessRecord) => {'))
  const guard = save.indexOf('if (staleEdit(editBaseRevision.current, item.card_revision, editRefusedStale))')
  const request = save.indexOf('dispatch(updateDelegatedAccess(')
  assert.ok(guard > 0 && guard < request, 'the stale check runs before any request')
  assert.match(save.slice(guard, request), /setEditActionError\(STALE_EDIT_MESSAGE\);\s+return;/)
  assert.match(save, /if \(updated\?\.status === 409\) \{\s+editBaseRevision\.current = pinAfterRefusal\(editBaseRevision\.current\);\s+setEditRefusedStale\(true\);/)
  assert.doesNotMatch(panel, /editBaseRevision\.current = null;\s+setEditActionError/)
  assert.doesNotMatch(panel, /if \(updated\?\.status === 409\) editBaseRevision\.current = null;/)
  assert.match(panel, /expectedCardRevision: editBaseRevision\.current \?\? item\.card_revision,/)
  // Save is disabled while stale, not merely warned about.
  assert.match(panel, /\|\| staleEdit\(editBaseRevision\.current, record\.card_revision, editRefusedStale\)/)
})

test('Reload this Card keeps the editor open, seeded from the server version with a new pin', () => {
  const reload = panel.slice(panel.indexOf('const reloadEdit = async'), panel.indexOf('startEdit(current);\n  };', panel.indexOf('const reloadEdit = async')) + 20)
  assert.match(reload, /const current = await readCurrentCard\(item\);[^]*?startEdit\(current\);/)
  assert.doesNotMatch(reload, /clearEditState\(\)/) // the editor stays open
  assert.match(panel, /editBaseRevision\.current = pinAtStart\(item\);\s+setEditRefusedStale\(false\);/)
  assert.match(panel, /onClick=\{\(\) => \{ void reloadEdit\(record\); \}\}>\s+Reload this Card/)
})

test('opening any Card reads it, and a list reload re-reads an open Card the list does not hold', () => {
  assert.match(panel, /if \(!viewAccessId\) return;\s+const record = freshestCard\(items, focusedCard, viewAccessId\);\s+if \(record\) void readCurrentCard\(record\);/)
  const listReload = panel.slice(panel.indexOf('const openCardId = editingAccessId || viewAccessId;'))
  assert.match(listReload, /items\.some\(\(item\) => item\.access_id === openCardId\)\) return;/)
  assert.match(listReload, /record\.source === 'control' \|\| projectAgentCardUpdateTarget\(record\)/)
})
