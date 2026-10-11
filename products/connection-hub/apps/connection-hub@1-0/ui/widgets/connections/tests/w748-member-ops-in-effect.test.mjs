import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { withManagedGrantsAsHeld } from '../src/features/delegatedAccess/managedGrants.ts'
import {
  hiddenHeldGrants, namedServicesShownOnPersonCard, resourceForPersonCard, resourcesForPersonMyCard,
} from '../src/features/delegatedAccess/personCardOperations.ts'

// W748. Operator, live on install 1 (AE 4a1b8056), 2026-10-11 about 17:46Z: "this is what i see. read is not
// ticked btw" (My Card: "Read project control Card - Set by the application - project.control.get", unticked).
// An operation no person Card decides shows whether it is IN EFFECT for the person: the application's declared
// membership operations ticked, every other one (agent-only, retired) unticked; still disabled. Save unchanged.

const table = JSON.parse(readFileSync(new URL('./fixtures/w731-pb-operations.json', import.meta.url), 'utf8'))
const UNDECIDED = [...table.absent, ...table.role_only]
const ROW = {
  resource: 'problem-board', person_card_operations: table.declared, person_member_operations: table.membership,
  operations: [...table.declared, ...UNDECIDED].sort().map((name) => ({
    name, grants: name === 'project.control.get' ? ['work:read'] : ['work:write'],
    ...(table.role_only.includes(name) ? { person_card: false } : {}),
  })),
}

for (const [card, shown] of [['Control', () => resourceForPersonCard(ROW)], ['My', () => resourcesForPersonMyCard([ROW])[0]]]) {
  test(`a person's ${card} Card marks the membership operations in effect and no other undecided one`, () => {
    const undecided = shown().operations.filter((op) => op.person_undecided)
    assert.equal(undecided.length, 49)
    assert.deepEqual(undecided.filter((op) => op.in_effect === true).map((op) => op.name).sort(), [...table.membership].sort())
    assert.equal(undecided.find((op) => op.name === 'project.control.get').in_effect, true)
    assert.equal(undecided.find((op) => op.name === 'worker.publish').in_effect, false)
    assert.equal(shown().operations.filter((op) => !op.person_undecided).some((op) => 'in_effect' in op), false)
  })
}

test('without a membership declaration no in-effect mark is set (the tick shows what the Card holds)', () => {
  const { person_member_operations: _, ...plain } = ROW
  assert.equal(resourceForPersonCard(plain).operations.some((op) => 'in_effect' in op), false)
})

test('a named alias is in effect when its operation is a membership operation', () => {
  const named = { ...ROW, named_services: [{ namespace: 'work', tools: { 'object.action': { operations: Object.fromEntries(
    table.alias_role_only.map((name) => [name, { person_card: false }])) } } }] }
  const operations = namedServicesShownOnPersonCard(named).named_services[0].tools['object.action'].operations
  assert.deepEqual(Object.entries(operations).filter(([, entry]) => entry.in_effect).map(([name]) => name).sort(),
    table.alias_role_only.filter((name) => table.membership.includes(name.replace('object.action.', ''))).sort())
})

test('the in-effect tick never reaches Save: the payload is byte-identical with or without the declaration', () => {
  const { person_member_operations: _, ...plain } = ROW
  const payload = (row) => {
    const heldOperations = ['plan.item.update', 'project.control.get', 'worker.publish', 'review.assign']
    const managed = new Set(row.operations.filter((op) => op.managed).map((op) => op.name))
    return JSON.stringify({
      ops: withManagedGrantsAsHeld({ 'problem-board': ['plan.item.update'] }, { 'problem-board': heldOperations }, () => managed),
      grants: hiddenHeldGrants(row, heldOperations, ['work:read', 'work:write']),
    })
  }
  assert.equal(payload(resourceForPersonCard(ROW)), payload(resourceForPersonCard(plain)))
  assert.equal(payload(resourcesForPersonMyCard([ROW])[0]), payload(resourcesForPersonMyCard([plain])[0]))
})
