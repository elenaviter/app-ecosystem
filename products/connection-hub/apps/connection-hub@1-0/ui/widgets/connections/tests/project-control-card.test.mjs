import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { accessCardFocusFromParams } from '../src/features/delegatedAccess/accessCardFocus.ts'
import { controlCardGetRequest } from '../src/features/delegatedAccess/projectPersonControl.ts'
import {
  PROJECT_CONTROL_CARD_READ_ONLY_MESSAGE,
  projectControlCardFocus,
  projectControlCardReadOnly,
  projectControlCardRecord,
  projectControlCardUpdateTarget,
} from '../src/features/delegatedAccess/projectControlCard.ts'

// W260 live check: a second project admin, who did not create the project's
// Control Card, pressed "Manage project access" on the board and read
// "control_card_not_found": the widget asked the creator-only
// `control_card_get`. A Control Card link that names the project now reads and
// saves through the project path.

function focus(values) {
  return accessCardFocusFromParams((key) => String(values[key] || ''))
}

const source = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8')

const PROJECT = 'work:project:one'
const LINK = { control_card_id: 'control-project', project_ref: PROJECT }

test('the Manage project access link reads the Control Card through its project', () => {
  const opened = focus(LINK)
  assert.deepEqual(projectControlCardFocus(opened), { controlId: 'control-project', projectRef: PROJECT })
  assert.deepEqual(controlCardGetRequest({ controlId: opened.accessId, projectRef: opened.projectRef }), {
    operation: 'project_control_card_get',
    data: { control_id: 'control-project', project_ref: PROJECT },
  })
})

test("a person's Control Card and a plain Control Card keep their own paths", () => {
  const person = focus({ ...LINK, target_subject: 'platform-user-2' })
  assert.equal(projectControlCardFocus(person), null)
  assert.equal(
    controlCardGetRequest({ controlId: person.accessId, projectRef: person.projectRef, targetSubject: person.targetSubject }).operation,
    'project_person_control_get',
  )
  assert.equal(projectControlCardFocus(focus({ control_card_id: 'control-own' })), null)
  assert.equal(controlCardGetRequest({ controlId: 'control-own' }).operation, 'control_card_get')
  assert.equal(projectControlCardFocus(focus({ access_id: 'aut_agent', project_ref: PROJECT })), null)
})

test('a second project admin who did not create the Card opens it and saves through the project', () => {
  // What project_control_card_get answers: the Card, with how the project let this person in.
  const record = projectControlCardRecord({
    access_id: 'control-project',
    source: 'control',
    label: 'Project access',
    via: 'project_admin',
    can_edit: true,
    project_ref: PROJECT,
  })
  assert.equal(record.access_id, 'control-project')
  assert.equal(record.source, 'control')
  assert.deepEqual(record.project_control_card, { via: 'project_admin', can_edit: true, project_ref: PROJECT })
  assert.equal(projectControlCardReadOnly(record), false)
  assert.deepEqual(projectControlCardUpdateTarget(record), { controlId: 'control-project', projectRef: PROJECT })

  // The save goes to project_control_card_update with the Card and the project.
  const slice = source('src/features/delegatedAccess/delegatedAccessSlice.ts')
  assert.match(slice, /projectControlCard\s*\n\s*\? 'project_control_card_update'/)
  assert.match(slice, /control_id: projectControlCard\.controlId, project_ref: projectControlCard\.projectRef/)
  assert.match(slice, /request\.operation === 'project_control_card_get'\s*\n\s*\? \{ \.\.\.res, access: projectControlCardRecord\(res\.access\) \}/)
  const panel = source('src/features/delegatedAccess/DelegatedAccessPanel.tsx')
  assert.match(panel, /const projectControlCard = projectControlCardUpdateTarget\(item\)/)
  assert.match(panel, /projectControlCard: projectControlCard \|\| undefined/)
})

test('a project member reads the Card and is told who changes it', () => {
  const record = projectControlCardRecord({
    access_id: 'control-project', source: 'control', via: 'project_member', can_edit: false, project_ref: PROJECT,
  })
  assert.equal(projectControlCardReadOnly(record), true)
  assert.match(PROJECT_CONTROL_CARD_READ_ONLY_MESSAGE, /A project admin changes this project's Control Card/)
  const panel = source('src/features/delegatedAccess/DelegatedAccessPanel.tsx')
  // One read-only rule for every project path, checked before anything is written (H1).
  assert.match(panel, /const readOnlyReason = cardReadOnlyReason\(item, focusedViewer\);/)
  assert.match(source('src/features/delegatedAccess/cardEditability.ts'), /if \(projectControlCardReadOnly\(item\)\) return PROJECT_CONTROL_CARD_READ_ONLY_MESSAGE;/)
})

test('a Card from the creator path carries no project route', () => {
  assert.equal(projectControlCardUpdateTarget({ access_id: 'control-own', source: 'control' }), null)
  assert.equal(projectControlCardReadOnly({ access_id: 'control-own', source: 'control' }), false)
})
