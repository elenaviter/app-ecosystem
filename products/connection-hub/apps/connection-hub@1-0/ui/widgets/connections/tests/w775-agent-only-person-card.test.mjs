import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { withManagedGrantsAsHeld } from '../src/features/delegatedAccess/managedGrants.ts'
import {
  agentOnlyOnPersonCard, hiddenHeldGrants, resourceForPersonCard, resourcesForPersonMyCard, splitAgentOnly,
} from '../src/features/delegatedAccess/personCardOperations.ts'

// W775. Operator, 2026-10-13 (about 06:40Z): "(b) on a person's Card in Connection Hub, fold the agent-only tools
// under \"Agent-only tools (not for people)\" without the amber INACTIVE warning. << b) - yes." What is agent-only
// comes from the application's `person_agent_operations` (Problem Board: operation_rules.py "agent_channel" and
// "shared_write_agents_only"); the Hub keeps no list of its own. Display only: Save is unchanged.

const table = JSON.parse(readFileSync(new URL('./fixtures/w731-pb-operations.json', import.meta.url), 'utf8'))
const AGENT = ['assignment.report', 'attachment.request_upload', 'control.acknowledge', 'control.discard_complete',
  'control.pull', 'control.refuse', 'control.worker_settle', 'event.publish', 'journal.view.fail', 'journal.view.publish',
  'mail.reconciliation.publish', 'mail.route', 'note.view.fail', 'note.view.publish', 'operation.receipt.get',
  'plan.nodes.publish', 'project.coordinator.note.write', 'project.file.edit.result', 'project.github.use',
  'project.role.handover.decide', 'session.resume.fail', 'session.resume.publish', 'worker.heartbeat', 'worker.publish',
  'workspace.shared_write.clear', 'workspace.shared_write.publish']
const ROW = {
  resource: 'problem-board', person_card_operations: table.declared, person_member_operations: table.membership,
  person_agent_operations: AGENT,
  operations: [...table.declared, ...table.absent, ...table.role_only].sort().map((name) => ({
    name, grants: ['work:write'], ...(table.role_only.includes(name) ? { person_card: false } : {}),
  })),
}

test('the fixture names agent-only operations the W731 table lists and no person-decided or membership one', () => {
  const all = new Set(ROW.operations.map((op) => op.name))
  assert.ok(AGENT.every((name) => all.has(name)))
  assert.ok(!AGENT.some((name) => table.declared.includes(name) || table.membership.includes(name)))
})

for (const [card, shown] of [['Control', () => resourceForPersonCard(ROW)], ['My', () => resourcesForPersonMyCard([ROW])[0]]]) {
  test(`a person's ${card} Card marks exactly the declared agent-only operations, and they fold away`, () => {
    const operations = shown().operations
    assert.deepEqual(operations.filter((op) => op.agent_only).map((op) => op.name).sort(), [...AGENT].sort())
    assert.ok(operations.filter((op) => op.agent_only).every((op) => op.managed && op.person_undecided && op.in_effect === false))
    const { regular, agentOnly } = splitAgentOnly(operations)
    assert.equal(agentOnly.length, AGENT.length)
    assert.equal(regular.length + agentOnly.length, operations.length, 'nothing is dropped, only folded')
    assert.ok(regular.some((op) => op.name === 'project.control.get') && regular.some((op) => op.name === 'plan.item.update'))
  })
}

test("an agent's Card (or any non-person Card) sees the catalog unchanged: nothing is agent_only", () => {
  // The panel gives non-person Cards the catalog rows as served; only the person helpers set the mark.
  assert.equal(ROW.operations.some((op) => 'agent_only' in op), false)
  assert.deepEqual(splitAgentOnly(ROW.operations).agentOnly, [])
})

test('without a declaration nothing is agent-only (the Hub keeps no list of its own)', () => {
  const { person_agent_operations: _, ...plain } = ROW
  assert.equal(resourceForPersonCard(plain).operations.some((op) => 'agent_only' in op), false)
  assert.equal(agentOnlyOnPersonCard(plain, 'worker.publish'), false)
  assert.equal(agentOnlyOnPersonCard(ROW, 'worker.publish'), true)
  assert.equal(agentOnlyOnPersonCard(ROW, 'plan.item.update'), false)
  assert.equal(agentOnlyOnPersonCard(undefined, 'worker.publish'), false)
})

test('the fold never reaches Save: the payload is byte-identical with or without the declaration', () => {
  const { person_agent_operations: _, ...plain } = ROW
  const payload = (row) => {
    const heldOperations = ['plan.item.update', 'project.control.get', 'worker.publish', 'mail.route']
    const managed = new Set(row.operations.filter((op) => op.managed).map((op) => op.name))
    return JSON.stringify({
      ops: withManagedGrantsAsHeld({ 'problem-board': ['plan.item.update'] }, { 'problem-board': heldOperations }, () => managed),
      grants: hiddenHeldGrants(row, heldOperations, ['work:read', 'work:write']),
    })
  }
  assert.equal(payload(resourceForPersonCard(ROW)), payload(resourceForPersonCard(plain)))
  assert.equal(payload(resourcesForPersonMyCard([ROW])[0]), payload(resourcesForPersonMyCard([plain])[0]))
})
