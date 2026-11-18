import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { controlNarrowingRows, controlNarrowingSource } from '../src/features/delegatedAccess/controlNarrowing.ts'

const NAMED = 'https://hub/mcp/named-services'

test('W38: the notice lists each choice the Control no longer allows, operations before permissions', () => {
  const rows = controlNarrowingRows({
    by: 'administrator',
    named_service_operations: { [NAMED]: { docs: ['object.action.insert_page_break', 'object.action.export'] } },
    outer_operations: { [NAMED]: ['named_services_action'] },
    claims: { [NAMED]: ['docs:write'] },
  })
  assert.deepEqual(rows, [
    { resource: NAMED, namespace: 'docs', operation: 'object.action.insert_page_break' },
    { resource: NAMED, namespace: 'docs', operation: 'object.action.export' },
    { resource: NAMED, operation: 'named_services_action' },
    { resource: NAMED, claim: 'docs:write' },
  ])
})

test('W38: the notice says who narrowed the Control', () => {
  assert.match(controlNarrowingSource({ by: 'administrator' }), /administrator/)
  assert.match(controlNarrowingSource({ by: 'descriptor' }), /descriptor/)
  assert.deepEqual(controlNarrowingRows(undefined), [])
})

test('W38: the editor and the listing render the notice beside the catalog drift, from server data only', () => {
  const panel = readFileSync(new URL('../src/features/delegatedAccess/DelegatedAccessPanel.tsx', import.meta.url), 'utf8')
  assert.equal((panel.match(/<ControlNarrowingNotice/g) || []).length, 2)
  assert.match(panel, /outside=\{linkedControlCard\(record\)\?\.outside_control\}/)
  assert.match(panel, /outside=\{linkedControlCard\(item\)\?\.outside_control\}/)
  assert.match(panel, /The last update from this agent's descriptor was refused/)
  // An outside-Control row never says a Save removes it (kdcube#270 item 10).
  const notice = panel.slice(panel.indexOf('function ControlNarrowingNotice'), panel.indexOf('// W587'))
  assert.match(notice, /stays on this Card when you save/)
})
