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
  roleDecidedHeld,
  roleDecidedResources,
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

// W560, operator 2026-10-05: "if there are operations that the people cannot
// edit (they either on or off absed on role, alltogether) then they still must
// be shown on the card but made non-editable. simply seletced and non-editable".
test('the role-decided operations are listed for the Card, not dropped', () => {
  const rows = roleDecidedResources([ROW, { resource: 'other', operations: [{ name: 'read' }] }])
  assert.deepEqual(rows.map((row) => row.resource), ['problem-board'])
  assert.deepEqual(
    rows[0].operations.map((operation) => operation.name),
    ['project.coordinator.hand_over', 'project.people.invite'],
  )
  // The editor still offers them nowhere, so a save never adds or removes one.
  assert.deepEqual(
    resourceForPersonCard(ROW).operations.map((operation) => operation.name),
    ['review.assign', 'plan.item.create', 'project.plan.index'],
  )
})

test('they are held by an admin, not by a member, and unknown without a role', () => {
  assert.equal(roleDecidedHeld({ known: true, role: 'admin', administers: true }), true)
  assert.equal(roleDecidedHeld({ known: true, role: 'owner', administers: true }), true)
  assert.equal(roleDecidedHeld({ known: true, role: 'member', administers: false }), false)
  assert.equal(roleDecidedHeld({ known: false, role: '', administers: false }), null)
  assert.equal(roleDecidedHeld(undefined), null)
})

test('the Card renders them ticked by role and disabled', () => {
  const source = readFileSync(new URL('../src/features/delegatedAccess/RoleDecidedOperations.tsx', import.meta.url), 'utf8')
  assert.match(source, /<input type="checkbox" checked=\{held === true\} disabled readOnly \/>/)
  const panel = readFileSync(new URL('../src/features/delegatedAccess/DelegatedAccessPanel.tsx', import.meta.url), 'utf8')
  assert.equal((panel.match(/renderRoleDecidedOperations\((record|item)\)/g) || []).length, 2, 'editor and read-only view')
})
