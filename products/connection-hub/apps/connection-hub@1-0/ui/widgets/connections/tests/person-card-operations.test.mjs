import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  catalogDriftForPersonCard,
  driftForPersonCard,
  notOfferedNamedOnPersonCard,
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

// W560 (operator, 2026-10-05: "they still must be shown on the card but made non-editable. simply seletced
// and non-editable"): the role-only outer operations stay listed, marked managed (shown as the Card holds
// them, never changed by the editor or its Save). Named-service tools keep the W360 narrowing for now.
test('a person\'s Control Card lists every outer operation; the role-only ones are managed, not hidden', () => {
  const offered = resourceForPersonCard(ROW)
  assert.deepEqual(offered.operations.map((op) => op.name), ROW.operations.map((op) => op.name))
  assert.deepEqual(offered.operations.filter((op) => op.managed).map((op) => op.name).sort(),
    ['project.coordinator.hand_over', 'project.people.invite'])
  assert.deepEqual(offered.operations.filter((op) => !op.managed).map((op) => op.name),
    ['review.assign', 'plan.item.create', 'project.plan.index'])
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
  assert.match(parts, /state = driftForPersonCard\(state, notOffered\);/)
  assert.equal((panel.match(/<CatalogDriftNotice drift=\{cardCatalogDrift\((record|item)\)\} \/>/g) || []).length, 2)
  assert.doesNotMatch(panel, /<CatalogDriftNotice drift=\{(record|item)\.catalog_drift\} \/>/)
})

// Operator, 2026-09-27: after the board's catalog changed, the LinkedIn admin's
// person Control Card listed project.people.invite and set_role as "changed,
// suspended until you accept". Both are marked role-only: the drift review of
// a person's Card lists them neither as changed nor as newly advertised.
test('a person\'s Card drift review hides marked operations, changed and added, and keeps the rest', () => {
  const state = {
    status: 'changed',
    changed_operations: ['project.people.invite', 'review.assign'],
    added_operations: ['project.coordinator.hand_over', 'plan.item.create'],
    removed_operations: ['project.people.invite'],
  }
  const shown = driftForPersonCard(state, notOfferedOnPersonCard(ROW))
  assert.deepEqual(shown.changed_operations, ['review.assign'])
  assert.deepEqual(shown.added_operations, ['plan.item.create'])
  assert.deepEqual(shown.removed_operations, ['project.people.invite'], 'what the service no longer offers stays listed')
  assert.equal(state.changed_operations.length, 2, 'the server\'s drift is not mutated')
  // Only role-only changes: the review has nothing to show.
  const onlyMarked = driftForPersonCard(
    { status: 'changed', changed_operations: ['project.people.invite'] },
    notOfferedOnPersonCard(ROW),
  )
  assert.deepEqual(onlyMarked.changed_operations, [])
  assert.equal(driftForPersonCard(state, undefined), state, 'another Card sees the drift as the server sent it')
  assert.equal(driftForPersonCard(undefined, ['x']), undefined)
})

test('the Card-level notice of a person\'s Card leaves out marked outer and named-service additions', () => {
  assert.deepEqual(
    [...notOfferedNamedOnPersonCard(ROW)].sort(),
    ['work object.action.project.coordinator.get', 'work object.action.project.coordinator.make'],
  )
  const drift = {
    status: 'changed',
    added: {
      claims: [{ resource: 'problem-board', claim: 'plan' }],
      outer_operations: [
        { resource: 'problem-board', operation: 'project.people.invite' },
        { resource: 'problem-board', operation: 'review.assign' },
        { resource: 'other', operation: 'project.people.invite' },
      ],
      named_service_operations: [
        { resource: 'problem-board', namespace: 'work', operation: 'object.action.project.coordinator.make' },
        { resource: 'problem-board', namespace: 'work', operation: 'object.action.review.assign' },
      ],
    },
  }
  const shown = catalogDriftForPersonCard(drift, (resource) => (resource === 'problem-board' ? ROW : undefined))
  assert.deepEqual(shown.added.outer_operations.map((row) => `${row.resource} ${row.operation}`), [
    'problem-board review.assign',
    'other project.people.invite',
  ])
  assert.deepEqual(shown.added.named_service_operations.map((row) => row.operation), ['object.action.review.assign'])
  assert.deepEqual(shown.added.claims, drift.added.claims)
  assert.equal(catalogDriftForPersonCard(undefined, () => ROW), undefined)
})
