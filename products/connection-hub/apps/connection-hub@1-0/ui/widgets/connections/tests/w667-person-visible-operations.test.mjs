import assert from 'node:assert/strict'
import test from 'node:test'

import { withManagedGrantsAsHeld } from '../src/features/delegatedAccess/managedGrants.ts'
import {
  hiddenHeldGrants,
  notOfferedOnPersonCard,
  resourceForPersonCard,
  resourcesForPersonMyCard,
  visibleOperations,
} from '../src/features/delegatedAccess/personCardOperations.ts'

// Operator, 2026-10-09 (W667): "eveyrthing that is not controlled and cannot be controlled must be
// hidden!"; managed role-decided operations: "not viisble." The application declares which operations a
// person's Card decides (`person_card_operations`); a person's Card shows only those, minus the
// role-decided (`person_card: false`) ones. Hiding never edits a Card.

const ROW = {
  resource: 'problem-board',
  person_card_operations: ['plan.item.create', 'review.assign', 'project.people.invite'],
  operations: [
    { name: 'review.assign', group: 'review' },
    { name: 'plan.item.create', group: 'plan' },
    { name: 'plan.notes.list', group: 'plan', grants: ['work:read'] },  // decided by membership: not declared
    { name: 'project.github.use', group: 'project' },      // agent channel: not declared
    { name: 'project.people.invite', group: 'people', person_card: false },  // managed, even if declared
  ],
}

test('a person Card shows only the declared operations it decides', () => {
  const shown = visibleOperations(resourceForPersonCard(ROW).operations).map((operation) => operation.name)
  assert.deepEqual(shown, ['review.assign', 'plan.item.create'])
})

test('hidden operations are kept exactly as the Card holds them on Save', () => {
  const row = resourceForPersonCard(ROW)
  const held = { 'problem-board': ['review.assign', 'plan.notes.list', 'project.github.use', 'project.people.invite'] }
  // The editor's selection only ever contains visible operations; the person unticked review.assign.
  const selected = { 'problem-board': ['plan.item.create'] }
  const saved = withManagedGrantsAsHeld(selected, held, () => new Set(
    row.operations.filter((operation) => operation.managed).map((operation) => operation.name)))
  assert.deepEqual(new Set(saved['problem-board']),
    new Set(['plan.item.create', 'plan.notes.list', 'project.github.use', 'project.people.invite']))
})

test('the drift review leaves out every hidden operation', () => {
  assert.deepEqual(notOfferedOnPersonCard(ROW).sort(), ['plan.notes.list', 'project.github.use', 'project.people.invite'])
})

// W560 (operator ruling relayed 2026-10-09: managed operations "not viisble"; everything that cannot be
// controlled "must be hidden"). This supersedes the earlier disabled "Set by the application" display on
// person Cards: a `person_card: false` operation is hidden whether or not the application declares anything.
for (const [label, declaration] of [['without a declaration', undefined], ['with an empty declaration', []]]) {
  test(`${label} the managed operation is hidden on a person's Control Card and the rest is shown`, () => {
    const { person_card_operations: _, ...base } = ROW
    const row = resourceForPersonCard(declaration === undefined ? base : { ...base, person_card_operations: declaration })
    assert.deepEqual(visibleOperations(row.operations).map((operation) => operation.name),
      ['review.assign', 'plan.item.create', 'plan.notes.list', 'project.github.use'])
    assert.deepEqual(row.operations.filter((operation) => operation.managed && operation.hidden).map((operation) => operation.name),
      ['project.people.invite'])
  })

  test(`${label} a person's My Card hides only the managed operation`, () => {
    const { person_card_operations: _, ...base } = ROW
    const named = [{ namespace: 'work', tools: { action: { operations: { 'object.action.set_role': { person_card: false } } } } }]
    const plain = { ...base, named_services: named, ...(declaration === undefined ? {} : { person_card_operations: declaration }) }
    const [mine] = resourcesForPersonMyCard([plain])
    assert.deepEqual(visibleOperations(mine.operations).map((operation) => operation.name),
      ['review.assign', 'plan.item.create', 'plan.notes.list', 'project.github.use'])
    assert.deepEqual(mine.named_services, named)  // named services on a My Card are shown as before
  })
}

test('a My Card row with no managed operation and no declaration is returned unchanged', () => {
  const plain = { resource: 'other', operations: [{ name: 'read' }, { name: 'write' }] }
  const [mine] = resourcesForPersonMyCard([plain])
  assert.equal(mine, plain)
})

test('without a declaration Save still keeps a held managed operation and its grant exactly as held', () => {
  const { person_card_operations: _, ...undeclared } = ROW
  const row = resourceForPersonCard({ ...undeclared, operations: undeclared.operations.map((operation) =>
    operation.name === 'project.people.invite' ? { ...operation, grants: ['work:admin'] } : operation) })
  const held = { 'problem-board': ['review.assign', 'project.people.invite'] }
  const saved = withManagedGrantsAsHeld({ 'problem-board': ['plan.item.create'] }, held, () => new Set(
    row.operations.filter((operation) => operation.managed).map((operation) => operation.name)))
  assert.deepEqual(new Set(saved['problem-board']), new Set(['plan.item.create', 'project.people.invite']))
  assert.deepEqual(hiddenHeldGrants(row, ['project.people.invite'], ['work:admin', 'work:read']), ['work:admin'])
  assert.deepEqual(hiddenHeldGrants(row, [], ['work:admin']), [])  // not held: never kept or added
})

test("a person's My Card hides the same set when declared; an agent Card is never filtered here", () => {
  const [mine] = resourcesForPersonMyCard([ROW])
  assert.deepEqual(visibleOperations(mine.operations).map((operation) => operation.name), ['review.assign', 'plan.item.create'])
  assert.equal(visibleOperations(ROW.operations).length, ROW.operations.length)  // the raw catalog (agent Cards)
})

test('a grant only a hidden held operation needs is kept exactly as held on Save', () => {
  const row = resourceForPersonCard(ROW)
  assert.deepEqual(hiddenHeldGrants(row, ['plan.notes.list', 'review.assign'], ['work:read', 'work:write']), ['work:read'])
  assert.deepEqual(hiddenHeldGrants(row, ['review.assign'], ['work:read']), [])  // not held: nothing kept
  assert.deepEqual(hiddenHeldGrants(row, ['plan.notes.list'], []), [])          // never adds a grant not held
})
