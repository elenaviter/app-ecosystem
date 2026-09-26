import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { OTHER_GROUP_LABEL, groupOperations } from '../src/features/delegatedAccess/operationGroups.ts'
import { cardReadOnlyReason } from '../src/features/delegatedAccess/cardEditability.ts'

// W260 (operator ruling, 2026-09-26): Connection Hub is the only Card editor,
// and the service catalog declares how operations are grouped.

const source = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8')

const ops = [
  { name: 'work.report', group: 'work' },
  { name: 'review.approve', group: 'review' },
  { name: 'people.list' },
  { name: 'plan.update', group: 'plan' },
  { name: 'review.assign', group: 'review' },
]
const declared = [
  { group: 'review', label: 'Review', order: 10 },
  { group: 'work', label: 'Work', order: 20 },
]

test('operations group in the declared order, keep catalog order inside, and the rest go last', () => {
  const groups = groupOperations(ops, (op) => op.group, declared)
  assert.deepEqual(groups.map((group) => [group.label, group.items.map((op) => op.name)]), [
    ['Review', ['review.approve', 'review.assign']],
    ['Work', ['work.report']],
    ['plan', ['plan.update']],
    [OTHER_GROUP_LABEL, ['people.list']],
  ])
})

test('a service that declares no groups keeps its plain list', () => {
  assert.equal(groupOperations([{ name: 'a' }, { name: 'b' }], (op) => op.group, declared), null)
  assert.equal(groupOperations([], (op) => op.group, undefined), null)
})

test('a declared group with no offered operation is not shown', () => {
  const groups = groupOperations([{ name: 'work.report', group: 'work' }], (op) => op.group, declared)
  assert.deepEqual(groups.map((group) => group.key), ['work'])
})

test('all three operation lists render through the declared groups', () => {
  const panel = source('src/features/delegatedAccess/DelegatedAccessPanel.tsx')
  assert.match(panel, /renderOperationGroups\(item\.operations, \(operation\) => operation\.group, item\.operation_groups,/)
  assert.match(panel, /renderOperationGroups\(resourceOption\.operations, \(operation\) => operation\.group, resourceOption\.operation_groups,/)
  const catalog = source('src/features/delegatedAccess/DelegatedResourceCatalog.tsx')
  assert.match(catalog, /renderOperationGroups\(rows, \(row\) => row\.group, namespace\.operation_groups,/)
  assert.match(catalog, /group: String\(policy\.group \|\| tool\.group \|\| ''\)/)
})

const PERSON = {
  access_id: 'control-person',
  source: 'control',
  properties: {
    'connection_hub.project_person_control': {
      schema: 'connection_hub.project_person_control.v1',
      project_ref: 'work:project:one',
      target_subject: 'person-2',
    },
  },
}

test('a project admin edits a person Control Card here; anyone else reads it', () => {
  assert.equal(cardReadOnlyReason(PERSON, { can_edit: true }), '')
  assert.match(cardReadOnlyReason(PERSON, { can_edit: false }), /A project admin decides this Control Card/)
  assert.match(cardReadOnlyReason(PERSON, undefined), /A project admin decides/)
})

test('the agent Card and project Control Card paths read the same way up front', () => {
  const agent = { access_id: 'aut_1', project_agent_card: { via: 'platform_admin', can_edit: false, project_ref: 'p' } }
  assert.match(cardReadOnlyReason(agent, undefined), /platform admin/)
  assert.equal(cardReadOnlyReason({ ...agent, project_agent_card: { via: 'project_admin', can_edit: true, project_ref: 'p' } }, undefined), '')
  const project = { access_id: 'control-project', source: 'control', project_control_card: { via: 'project_member', can_edit: false, project_ref: 'p' } }
  assert.match(cardReadOnlyReason(project, undefined), /A project admin changes this project's Control Card/)
  assert.equal(cardReadOnlyReason({ access_id: 'own', source: 'control' }, undefined), '')
})
