import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { bridgeForOperation, bridgeToAdd } from '../src/features/delegatedAccess/bridgeTools.ts'

const OFFERED = ['named_services_list', 'named_services_get', 'named_services_action']

test('W34 C: each operation family maps to its bridge, with the generic bridge as the fallback', () => {
  assert.equal(bridgeForOperation('object.action.insert_page_break', OFFERED), 'named_services_action')
  assert.equal(bridgeForOperation('object.get', OFFERED), 'named_services_get')
  assert.equal(bridgeForOperation('object.search', OFFERED), '')
  assert.equal(bridgeForOperation('object.search', ['named_services_call']), 'named_services_call')
})

test('W34 C: the bridge is ticked only when the draft lacks it and the Control allows it', () => {
  const op = 'object.action.insert_page_break'
  assert.equal(bridgeToAdd(op, { offered: OFFERED, selected: [] }), 'named_services_action')
  assert.equal(bridgeToAdd(op, { offered: OFFERED, selected: ['named_services_action'] }), '')
  assert.equal(bridgeToAdd(op, { offered: OFFERED, selected: [], controlAllowed: ['named_services_get'] }), '')
  assert.equal(bridgeToAdd(op, { offered: OFFERED, selected: [], controlAllowed: ['named_services_action'] }), 'named_services_action')
})

test('W34 C: ticking an operation ticks its bridge and says what it is for, leaving Once or Every time to the owner', () => {
  const panel = readFileSync(new URL('../src/features/delegatedAccess/DelegatedAccessPanel.tsx', import.meta.url), 'utf8')
  const toggle = panel.slice(panel.indexOf('const toggleEditNamedServiceOperation'), panel.indexOf('const clearEditState'))
  assert.match(toggle, /const bridge = bridgeToAdd\(operation,/)
  assert.match(toggle, /withLinkedOperation\(current, resource, bridge\)/)
  assert.doesNotMatch(toggle, /invocation|setEditInvocation/i)
  assert.match(panel, /data-testid="added-bridges-notice"/)
  assert.match(panel, /Choose Once or Every time for it before saving\./)
})
