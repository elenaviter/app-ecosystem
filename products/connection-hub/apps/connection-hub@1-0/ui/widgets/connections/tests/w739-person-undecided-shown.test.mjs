import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { withManagedGrantsAsHeld } from '../src/features/delegatedAccess/managedGrants.ts'
import {
  hiddenHeldGrants,
  namedServicesShownOnPersonCard,
  namedServicesShownOnPersonMyCard,
  resourceForPersonCard,
  resourcesForPersonMyCard,
  visibleOperations,
} from '../src/features/delegatedAccess/personCardOperations.ts'

// W739. Operator, on W731, 2026-10-11: "Cards hide project.control.get since it's a membership read no Card
// decides << we still show this (simply disabled)", "ie.. we must", "because for other permisisons that also
// card does not decide we still show them". A person's Control (C) and My Card show every operation the Card
// does not decide, disabled as "Set by the application"; no authority or Save change.
//
// The Problem Board outer operations below are Infra's W731 visibility table (visibility.md, sha256
// 489e6cc513aeff4e0cff34f052cb79803dedf73160f267875d1fb080158bbd52; Apps 4ee2be2b, AE 18dfef56): 94 outer
// operations, of which 49 were hidden on C and My (43 absent from person_card_operations, 6 person_card:false).

const ABSENT = ['assignment.list', 'assignment.report', 'attachment.request_upload', 'control.acknowledge',
  'control.discard_complete', 'control.pull', 'control.refuse', 'control.worker_settle', 'event.publish',
  'journal.view.fail', 'journal.view.publish', 'mail.reconciliation.list', 'mail.reconciliation.publish',
  'mail.reconciliation.read', 'mail.route', 'note.view.fail', 'note.view.publish', 'operation.receipt.get',
  'plan.nodes.publish', 'plan.notes.list', 'project.control.get', 'project.file.edit.result', 'project.github.use',
  'project.plan.embedding_status', 'project.plan.history', 'project.plan.index', 'project.plan.item',
  'project.plan.resolve', 'project.plan.search', 'project.register', 'project.report.fail',
  'project.report.publish', 'session.resume.fail', 'session.resume.publish', 'work.attachment.link',
  'worker.estimate', 'worker.heartbeat', 'worker.publish', 'worker.rename', 'worker.retire',
  'workspace.shared_write.clear', 'workspace.shared_write.list', 'workspace.shared_write.publish']
const ROLE_ONLY = ['project.coordinator.get', 'project.coordinator.note.write', 'project.people.card.update',
  'project.role.get', 'project.role.handover.decide', 'work.item.save']
const DECLARED = ['assignment.assign', 'assignment.return', 'control.discard', 'control.enqueue',
  'journal.view.close', 'journal.view.request', 'plan.index.embed', 'plan.item.create', 'plan.item.delete',
  'plan.item.update', 'plan.note.append', 'project.announcement.publish', 'project.cards.manage',
  'project.control.initialize', 'project.control.update', 'project.coordinator.hand_over',
  'project.coordinator.make', 'project.coordinator.make_worker', 'project.coordinator.return',
  'project.coordinator.set_away', 'project.files.edit', 'project.link_worker', 'project.people.invite',
  'project.people.set_role', 'project.plan.import', 'project.references.migrate', 'project.references.preview',
  'project.role.assign', 'project.role.declare', 'project.role.manage', 'project.set_commit_identity',
  'project.set_files', 'project.set_goal', 'project.set_journal_home', 'project.set_repositories',
  'project.unlink_worker', 'review.accept', 'review.assign', 'review.cancel', 'review.return', 'work.accept',
  'work.assignee.set', 'work.status.set', 'worker.evict', 'worker.restore']
const UNDECIDED = [...ABSENT, ...ROLE_ONLY].sort()

const GRANTS = {
  'project.control.get': ['work:read'], 'plan.notes.list': ['work:read'], 'work.item.save': ['work:write'],
  'project.coordinator.get': ['work:admin'], 'plan.item.update': ['work:write'], 'project.cards.manage': ['work:admin'],
}
const operation = (name) => ({
  name, group: name.split('.')[0],
  ...(ROLE_ONLY.includes(name) ? { person_card: false } : {}),
  ...(GRANTS[name] ? { grants: GRANTS[name] } : {}),
})
const PB = {
  resource: 'problem-board',
  person_card_operations: [...DECLARED],
  operations: [...DECLARED, ...ABSENT, ...ROLE_ONLY].sort().map(operation),
}

for (const [card, shown] of [['Control', () => resourceForPersonCard(PB)], ['My', () => resourcesForPersonMyCard([PB])[0]]]) {
  test(`a person's ${card} Card shows all 94 outer operations; the 49 it does not decide are disabled`, () => {
    const row = shown()
    assert.equal(PB.operations.length, 94)
    assert.deepEqual(visibleOperations(row.operations).map((op) => op.name), PB.operations.map((op) => op.name))
    assert.equal(row.operations.some((op) => op.hidden), false)
    const managed = row.operations.filter((op) => op.managed === true && op.person_undecided === true)
    assert.deepEqual(managed.map((op) => op.name).sort(), UNDECIDED)
    assert.equal(managed.length, 49)
    assert.ok(managed.some((op) => op.name === 'project.control.get'), 'the operator\'s named row is shown disabled')
  })

  test(`a person's ${card} Card leaves every editable row exactly as the catalog has it`, () => {
    const row = shown()
    const byName = new Map(PB.operations.map((op) => [op.name, op]))
    const editable = row.operations.filter((op) => !op.person_undecided)
    assert.deepEqual(editable.map((op) => op.name).sort(), [...DECLARED].sort())
    editable.forEach((op) => assert.equal(op, byName.get(op.name), `${op.name} is the catalog's own row`))
  })
}

// The rule before W739 (AE 18dfef56): the same 49 operations were `managed: true, hidden: true`.
const before = {
  ...PB,
  operations: PB.operations.map((op) => (UNDECIDED.includes(op.name) ? { ...op, managed: true, hidden: true } : op)),
}

function savePayload(row) {
  const heldOperations = ['plan.item.update', 'project.control.get', 'work.item.save', 'project.coordinator.get',
    'review.assign', 'project.cards.manage']
  const heldGrants = ['work:read', 'work:write', 'work:admin']
  const managed = new Set(row.operations.filter((op) => op.managed).map((op) => op.name))
  // The person unticked review.assign and ticked plan.item.create; managed rows are never in the selection.
  const selected = { 'problem-board': ['plan.item.update', 'plan.item.create', 'project.cards.manage'] }
  return JSON.stringify({
    resource_operations: withManagedGrantsAsHeld(selected, { 'problem-board': heldOperations }, () => managed),
    kept_grants: hiddenHeldGrants(row, heldOperations, heldGrants),
    kept_grants_none_held: hiddenHeldGrants(row, [], heldGrants),
    named_services: row.named_services,
  })
}

test('Save sends byte-identical outer operations and grants before and after W739', () => {
  for (const after of [resourceForPersonCard(PB), resourcesForPersonMyCard([PB])[0]]) {
    assert.equal(savePayload(after), savePayload(before))
  }
  const payload = JSON.parse(savePayload(resourceForPersonCard(PB)))
  assert.deepEqual(new Set(payload.resource_operations['problem-board']), new Set(['plan.item.update',
    'plan.item.create', 'project.cards.manage', 'project.control.get', 'work.item.save', 'project.coordinator.get']))
  assert.deepEqual(payload.kept_grants, ['work:read', 'work:write', 'work:admin'])
  assert.deepEqual(payload.kept_grants_none_held, [])
})

// C's six named-service aliases (W731 supplement): the helper left them out of a person's Control Card.
const ALIASES_ROLE_ONLY = ['object.action.work.item.save', 'object.action.project.people.card.update',
  'object.action.project.coordinator.get', 'object.action.project.role.get',
  'object.action.project.role.handover.decide', 'object.action.project.coordinator.note.write']
const NAMED = {
  resource: 'problem-board',
  named_services: [{
    namespace: 'work',
    tools: {
      'provider.about': { label: 'About' },
      'object.get': { label: 'Get' },
      'object.action': {
        operations: Object.fromEntries([
          'object.action.project.control.get', 'object.action.project.people.invite', 'object.action.review.assign',
          ...ALIASES_ROLE_ONLY,
        ].map((name) => [name, ALIASES_ROLE_ONLY.includes(name) ? { person_card: false } : {}])),
      },
    },
  }],
}

test('a person\'s Control Card shows its six undecided named-service aliases disabled; Save still omits them', () => {
  const shown = namedServicesShownOnPersonCard(NAMED).named_services[0].tools
  assert.deepEqual(Object.keys(shown), ['provider.about', 'object.get', 'object.action'])
  const operations = shown['object.action'].operations
  assert.equal(Object.keys(operations).length, 9)
  assert.deepEqual(Object.entries(operations).filter(([, entry]) => entry.person_undecided).map(([name]) => name).sort(),
    [...ALIASES_ROLE_ONLY].sort())
  assert.equal(shown['provider.about'], NAMED.named_services[0].tools['provider.about'], 'a decided tool is unchanged')
  // What Save encodes still comes from the narrowed resource: the six are not in it.
  const offered = resourceForPersonCard(NAMED).named_services[0].tools['object.action'].operations
  assert.deepEqual(Object.keys(offered), ['object.action.project.control.get', 'object.action.project.people.invite',
    'object.action.review.assign'])
  assert.equal(Object.values(offered).some((entry) => entry.person_undecided), false)
})

test('a tool marked as a whole is shown with every operation disabled', () => {
  const tool = { person_card: false, operations: { 'a.one': {}, 'a.two': { person_card: true } } }
  const plain = { person_card: false, label: 'Levers' }
  const row = { resource: 'r', named_services: [{ namespace: 'n', tools: { tool, plain } }] }
  const shown = namedServicesShownOnPersonCard(row).named_services[0].tools
  assert.deepEqual(Object.values(shown.tool.operations).map((entry) => entry.person_undecided), [true, true])
  assert.equal(shown.plain.person_undecided, true)
})

test('a My Card without a declaration shows its named services as before; with one, as the Control Card', () => {
  assert.equal(namedServicesShownOnPersonMyCard(NAMED), NAMED)
  const declared = { ...NAMED, person_card_operations: ['review.assign'] }
  assert.deepEqual(namedServicesShownOnPersonMyCard(declared), namedServicesShownOnPersonCard(declared))
})

test('the editor draws managed named rows disabled and never toggles, counts or sends them', () => {
  const catalog = readFileSync(new URL('../src/features/delegatedAccess/DelegatedResourceCatalog.tsx', import.meta.url), 'utf8')
  assert.match(catalog, /managed: policy\.person_undecided === true \|\| tool\.person_undecided === true/)
  assert.match(catalog, /operationRows\(namespace\)\.filter\(\(row\) => !row\.managed\)/)
  assert.match(catalog, /const rows = shownRows\.filter\(\(row\) => !row\.managed\);/)
  assert.match(catalog, /checked=\{row\.inEffect \?\? managedHeld\(namespace\.namespace, row\.operation\)\} disabled readOnly/)
  assert.match(catalog, /\{MANAGED_OPERATION_NOTE\}/)
  const panel = readFileSync(new URL('../src/features/delegatedAccess/DelegatedAccessPanel.tsx', import.meta.url), 'utf8')
  assert.match(panel, /resource=\{shownResourceOption \|\| resourceOption\}/)
  // Save's offered set still reads the narrowed resources, never the shown copy.
  assert.match(panel, /const offered = offeredNamedServiceOperations\(\n\s+resources, Object\.keys\(routedKept\), editRowFor,/)
  assert.doesNotMatch(panel, /offeredNamedServiceOperations\(\s*\[?shown/)
})
