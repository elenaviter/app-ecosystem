import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  notOfferedOnPersonCard,
  offeredOnPersonCard,
  resourceForPersonCard,
} from '../src/features/delegatedAccess/personCardOperations.ts'

// Operator, 2026-09-26 (W360): a person's Control Card offered the coordinator
// levers ("Newly advertised, not granted — Add to card"), which the board
// decides for a person by role alone. The catalog marks them
// `person_card: false`; a person's Control Card does not offer them, and every
// operation the board checks on a person's Card stays offered.

const ROW = {
  resource: 'problem-board',
  operations: [
    { name: 'review.assign', group: 'review' },
    { name: 'plan.item.create', group: 'plan' },
    { name: 'project.plan.index', group: 'plan' },
    { name: 'project.coordinator.hand_over', group: 'coordinator', person_card: false },
    { name: 'project.people.invite', group: 'people', person_card: false },
  ],
  named_services: [{
    namespace: 'work',
    tools: {
      get: { label: 'Get' },
      action: {
        operations: {
          'object.action.review.assign': { group: 'review' },
          'object.action.project.coordinator.make': { group: 'coordinator', person_card: false },
        },
      },
      levers: { operations: { 'object.action.project.coordinator.get': { person_card: false } } },
      hidden: { person_card: false },
    },
  }],
}

test('only an explicit false leaves an operation off a person\'s Card', () => {
  assert.equal(offeredOnPersonCard({}), true)
  assert.equal(offeredOnPersonCard({ person_card: true }), true)
  assert.equal(offeredOnPersonCard(undefined), true)
  assert.equal(offeredOnPersonCard({ person_card: false }), false)
})

test('a person\'s Control Card is offered every operation except the role-only ones', () => {
  const offered = resourceForPersonCard(ROW)
  assert.deepEqual(offered.operations.map((op) => op.name), ['review.assign', 'plan.item.create', 'project.plan.index'])
  const tools = offered.named_services[0].tools
  assert.deepEqual(Object.keys(tools), ['get', 'action'], 'a tool with no offered operation, or marked itself, is not offered')
  assert.deepEqual(Object.keys(tools.action.operations), ['object.action.review.assign'])
  // The catalog itself is untouched: other Cards see all of it.
  assert.equal(ROW.operations.length, 5)
  assert.equal(Object.keys(ROW.named_services[0].tools).length, 4)
})

test('newly advertised role-only operations are not listed on a person\'s Card', () => {
  assert.deepEqual(notOfferedOnPersonCard(ROW), ['project.coordinator.hand_over', 'project.people.invite'])
  assert.deepEqual(notOfferedOnPersonCard(undefined), [])
})

test('the editor narrows the catalog only for a project person\'s or invitation\'s Control Card', () => {
  const panel = readFileSync(new URL('../src/features/delegatedAccess/DelegatedAccessPanel.tsx', import.meta.url), 'utf8')
  assert.match(panel, /editingPersonControl \? resourcesForPersonCard\(catalogResources\) : catalogResources/)
  assert.match(panel, /return Boolean\(record && projectPersonControlCoordinates\(record\)\)/)
  assert.match(panel, /notOffered=\{projectPersonControlCoordinates\(item\)/)
  // The create form keeps the whole catalog.
  const create = panel.slice(panel.indexOf('const createResources = useMemo'), panel.indexOf('const createSelectionIndex'))
  assert.doesNotMatch(create, /\bresources\./)
  const parts = readFileSync(new URL('../src/features/delegatedAccess/ResourceEditorParts.tsx', import.meta.url), 'utf8')
  assert.match(parts, /added_operations: state\.added_operations\.filter\(\(operation\) => !hidden\.has\(operation\)\)/)
})
