import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { bridgeForOperation, bridgeToAdd, bridgeToRemove } from '../src/features/delegatedAccess/bridgeTools.ts'

const OFFERED = ['named_services_list', 'named_services_get', 'named_services_action']

test('each operation family maps to its bridge, with the generic bridge as the fallback', () => {
  assert.equal(bridgeForOperation('object.action.insert_page_break', OFFERED), 'named_services_action')
  assert.equal(bridgeForOperation('object.get', OFFERED), 'named_services_get')
  assert.equal(bridgeForOperation('object.search', OFFERED), '')
  assert.equal(bridgeForOperation('object.search', ['named_services_call']), 'named_services_call')
})

test('the bridge is ticked only when the draft lacks it and the Control allows it', () => {
  const op = 'object.action.insert_page_break'
  assert.equal(bridgeToAdd(op, { offered: OFFERED, selected: [] }), 'named_services_action')
  assert.equal(bridgeToAdd(op, { offered: OFFERED, selected: ['named_services_action'] }), '')
  assert.equal(bridgeToAdd(op, { offered: OFFERED, selected: [], controlAllowed: ['named_services_get'] }), '')
  assert.equal(bridgeToAdd(op, { offered: OFFERED, selected: [], controlAllowed: ['named_services_action'] }), 'named_services_action')
})

test('ticking an operation ticks its bridge and says what it is for, leaving Once or Every time to the owner', () => {
  const panel = readFileSync(new URL('../src/features/delegatedAccess/DelegatedAccessPanel.tsx', import.meta.url), 'utf8')
  const toggle = panel.slice(panel.indexOf('const toggleEditNamedServiceOperation'), panel.indexOf('const clearEditState'))
  assert.match(toggle, /const bridge = bridgeToAdd\(operation,/)
  assert.match(toggle, /withLinkedOperation\(current, resource, bridge\)/)
  assert.doesNotMatch(toggle, /invocation|setEditInvocation/i)
  assert.match(panel, /data-testid="added-bridges-notice"/)
  assert.match(panel, /Choose Once or Every time for it before saving\./)
})

test('no bridge is offered while the linked Control cannot be read', () => {
  const op = 'object.action.insert_page_break'
  assert.equal(bridgeToAdd(op, { offered: OFFERED, selected: [], controlUnavailable: true }), '')
})

test('unticking the last operation that needs a bridge unticks the bridge too', () => {
  const op = 'object.action.insert_page_break'
  const selected = ['named_services_action', 'named_services_get']
  assert.equal(bridgeToRemove(op, { offered: OFFERED, selected, stillTicked: ['object.get'] }), 'named_services_action')
  assert.equal(bridgeToRemove(op, { offered: OFFERED, selected, stillTicked: ['object.action.page_setup'] }), '',
    'another action still needs the same bridge')
  assert.equal(bridgeToRemove(op, { offered: OFFERED, selected: ['named_services_get'], stillTicked: [] }), '',
    'a bridge the draft does not hold is left alone')
})
