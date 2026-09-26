import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { accessCardFocusFromParams } from '../src/features/delegatedAccess/accessCardFocus.ts'
import {
  projectAgentCardFocus,
  projectAgentCardGetRequest,
  projectAgentCardReadOnly,
  projectAgentCardReadOnlyMessage,
  projectAgentCardRecord,
  projectAgentCardUpdateTarget,
} from '../src/features/delegatedAccess/projectAgentCard.ts'

// W319: a project's second admin opened an agent's Card link and read "Card
// aut_... does not exist or is not visible to this account": the Card lives
// under its owner. A link that names the project now opens it through that
// project, and Connection Hub asks the board who may read or change it.

function focus(values) {
  return accessCardFocusFromParams((key) => String(values[key] || ''))
}

const source = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8')

test('a plain agent Card link with a project takes the project path', () => {
  const target = projectAgentCardFocus(focus({ access_id: 'aut_agent', project_ref: 'work:project:one' }))
  assert.deepEqual(target, { accessId: 'aut_agent', projectRef: 'work:project:one' })
  assert.deepEqual(projectAgentCardGetRequest(target), {
    operation: 'project_agent_card_get',
    data: { access_id: 'aut_agent', project_ref: 'work:project:one' },
  })
})

test('control, manual and person links keep their own paths, and no project means none', () => {
  assert.equal(projectAgentCardFocus(focus({ access_id: 'aut_agent' })), null)
  assert.equal(projectAgentCardFocus(focus({ control_card_id: 'ctl', project_ref: 'work:project:one' })), null)
  assert.equal(projectAgentCardFocus(focus({ manual_access_id: 'm', project_ref: 'work:project:one' })), null)
  assert.equal(projectAgentCardFocus(focus({ access_id: 'a', project_ref: 'p', target_subject: 'boris' })), null)
  assert.equal(projectAgentCardFocus(null), null)
})

test('the opened record says how it was reached, and a save goes back the same way', () => {
  const admin = projectAgentCardRecord({
    ok: true,
    item: { access_id: 'aut_agent', label: 'agent' },
    access: { via: 'project_admin', can_edit: true, project_ref: 'work:project:one' },
  })
  assert.equal(admin.project_agent_card.via, 'project_admin')
  assert.deepEqual(projectAgentCardUpdateTarget(admin), { accessId: 'aut_agent', projectRef: 'work:project:one' })
  assert.equal(projectAgentCardReadOnly(admin), false)

  const platform = projectAgentCardRecord({
    item: { access_id: 'aut_agent' },
    access: { via: 'platform_admin', can_edit: false, project_ref: '' },
  })
  assert.equal(projectAgentCardReadOnly(platform), true)
  assert.match(projectAgentCardReadOnlyMessage(platform), /platform admin opens every agent Card/)

  assert.equal(projectAgentCardUpdateTarget({ access_id: 'own' }), null, 'an own Card saves the usual way')
  assert.equal(projectAgentCardRecord({ ok: true }), null)
})

test('the slice and the panel use the project operations', () => {
  const slice = source('src/features/delegatedAccess/delegatedAccessSlice.ts')
  assert.match(slice, /'project_agent_card_update'/)
  assert.match(slice, /loadProjectAgentCard = createAsyncThunk/)
  const panel = source('src/features/delegatedAccess/DelegatedAccessPanel.tsx')
  assert.match(panel, /dispatch\(loadProjectAgentCard\(target\)\)/)
  assert.match(panel, /projectAgentCard: projectAgentCard \|\| undefined/)
  assert.match(panel, /if \(projectAgentCardReadOnly\(item\)\)/)
})

// W319 slice 2: an agent its owner shared with the person opens without a
// project; Connection Hub decides the share (view read-only, edit changes).
test('a shared agent Card link takes the same path without a project', () => {
  const target = projectAgentCardFocus(focus({ access_id: 'aut_agent', shared: '1' }))
  assert.deepEqual(target, { accessId: 'aut_agent', projectRef: '' })
  assert.deepEqual(projectAgentCardGetRequest(target).data, { access_id: 'aut_agent', project_ref: '' })
  assert.equal(projectAgentCardFocus(focus({ access_id: 'aut_agent', shared: '0' })), null)
})

test('a view share opens read-only and says why; an edit share saves through the same path', () => {
  const viewing = projectAgentCardRecord({
    ok: true,
    item: { access_id: 'aut_agent' },
    access: { via: 'shared_view', can_edit: false, project_ref: '', worker_name: '' },
  })
  assert.equal(projectAgentCardReadOnly(viewing), true)
  assert.match(projectAgentCardReadOnlyMessage(viewing), /shares this agent with you to view/)
  const editing = projectAgentCardRecord({
    ok: true,
    item: { access_id: 'aut_agent' },
    access: { via: 'shared_edit', can_edit: true, project_ref: '', worker_name: '' },
  })
  assert.equal(projectAgentCardReadOnly(editing), false)
  assert.deepEqual(projectAgentCardUpdateTarget(editing), { accessId: 'aut_agent', projectRef: '' })
})
