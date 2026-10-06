// W587: a Card shown or edited is the current server revision, never an older cached copy.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  accessCardFocusFromParams,
  controlFocusKey,
  freshestCard,
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

test('returning to the tab reads the list and the open Control again', () => {
  const handler = panel.slice(panel.indexOf('const onVisible = () =>'), panel.indexOf("document.addEventListener('visibilitychange'"))
  assert.match(handler, /document\.visibilityState !== 'visible'/)
  assert.match(handler, /dispatch\(loadDelegatedAccess\(\)\)/)
  assert.match(handler, /dispatch\(loadControlCard\(/)
})

test('a save sends the revision the edit started from, so a refresh never makes it an overwrite', () => {
  assert.match(panel, /editBaseRevision\.current = item\.card_revision \?\? null;/)
  assert.match(panel, /expectedCardRevision: editBaseRevision\.current \?\? item\.card_revision,/)
  assert.match(panel, /if \(updated\?\.status === 409\) editBaseRevision\.current = null;/)
  assert.match(panel, /This Card was saved elsewhere/)
})
