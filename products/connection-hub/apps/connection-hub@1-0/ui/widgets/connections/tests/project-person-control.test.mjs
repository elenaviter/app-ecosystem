import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  controlCardGetRequest,
  isLinkedControlCard,
  linkedControlOpenTarget,
  projectPersonControlCoordinates,
} from '../src/features/delegatedAccess/projectPersonControl.ts'

test('Control Card reads use the bounded identity envelope for each route', () => {
  assert.deepEqual(controlCardGetRequest({
    controlId: 'control-person',
    projectRef: 'work:project:quickstart',
    targetSubject: 'platform-user-2',
  }), {
    operation: 'project_person_control_get',
    data: {
      project_ref: 'work:project:quickstart',
      target_subject: 'platform-user-2',
    },
  })
  assert.deepEqual(controlCardGetRequest({
    controlId: 'control-invitation',
    projectRef: 'work:project:quickstart',
    invitationRef: 'work:invitation:inv-1',
  }), {
    operation: 'project_person_control_get',
    data: {
      project_ref: 'work:project:quickstart',
      invitation_ref: 'work:invitation:inv-1',
      control_id: 'control-invitation',
    },
  })
  assert.deepEqual(controlCardGetRequest({ controlId: 'control-ordinary' }), {
    operation: 'control_card_get',
    data: { control_id: 'control-ordinary' },
  })
})

test('a project person Control Card exposes bounded editor coordinates', () => {
  assert.deepEqual(projectPersonControlCoordinates({
    properties: {
      'connection_hub.project_person_control': {
        schema: 'connection_hub.project_person_control.v1',
        project_ref: 'work:project:quickstart',
        target_subject: 'platform-user-2',
        project_subject: 'project-authority:opaque',
      },
    },
  }), {
    kind: 'person',
    projectRef: 'work:project:quickstart',
    targetSubject: 'platform-user-2',
  })
})

test('a pending invitation Control Card exposes invitation editor coordinates', () => {
  assert.deepEqual(projectPersonControlCoordinates({
    properties: {
      'connection_hub.project_invitation_control': {
        schema: 'connection_hub.project_invitation_control.v1',
        project_ref: 'work:project:quickstart',
        invitation_ref: 'work:invitation:inv-1',
        target_email_digest: 'opaque',
      },
    },
  }), {
    kind: 'invitation',
    projectRef: 'work:project:quickstart',
    invitationRef: 'work:invitation:inv-1',
  })
})

test('an ordinary Card has no project person route', () => {
  assert.equal(projectPersonControlCoordinates({ properties: {} }), null)
  assert.equal(projectPersonControlCoordinates({
    properties: {
      'connection_hub.project_person_control': {
        schema: 'connection_hub.project_person_control.v1',
        project_ref: 'work:project:quickstart',
      },
    },
  }), null)
  assert.equal(projectPersonControlCoordinates({
    properties: {
      'connection_hub.project_person_control': {
        schema: 'another.schema',
        project_ref: 'work:project:quickstart',
        target_subject: 'platform-user-2',
      },
    },
  }), null)
})

test('W260 scope: only a project-held person Control Card is read only; any other Control Card is not', async () => {
  const { projectPersonControlCoordinates } = await import('../src/features/delegatedAccess/projectPersonControl.ts')
  assert.equal(projectPersonControlCoordinates({ properties: {} }), null)
  assert.equal(projectPersonControlCoordinates({ properties: { 'connection_hub.agent_capability_control': {} } }), null)
})

// W424: "Open <Control Card>" on a person's My Card failed with
// control_card_not_found while the Team route opened the same Card: the
// button read the project-held Card as the signed-in person's own.
test('a My Card link opens the person project Control Card through the project and the person', () => {
  // claude-main's regression: an own My Card with a project-held binding
  // dispatches project_person_control_get {project_ref, target_subject: the viewer}.
  const myCard = {
    source: 'project-person', issuer_kind: 'project-person', issuer_ref: 'work:project:p',
    grantor_subject: 'viewer-subject', access_id: 'my-card-access-id',
  }
  const binding = { control_id: 'person-control-x', issuer_ref: 'work:project:p', issuer_kind: 'project', issuer_label: '42d5a4e4-e0e1-7040-f598-86c2845fae28' }
  const target = linkedControlOpenTarget(myCard, binding)
  assert.deepEqual(target, { controlId: 'person-control-x', projectRef: 'work:project:p', targetSubject: 'viewer-subject' })
  assert.deepEqual(controlCardGetRequest(target), {
    operation: 'project_person_control_get',
    data: { project_ref: 'work:project:p', target_subject: 'viewer-subject' },
  })
  // The key is the binding's control_id: never the label, never My Card's access_id.
  assert.equal(isLinkedControlCard({ access_id: 'person-control-x' }, target), true)
  assert.equal(isLinkedControlCard({ access_id: 'my-card-access-id' }, target), false)
  assert.equal(isLinkedControlCard({ access_id: '42d5a4e4-e0e1-7040-f598-86c2845fae28' }, target), false)
  assert.equal(isLinkedControlCard(null, target), false)
})

test('a My Card fails closed without a project ref or person; any other binding keeps control_card_get', () => {
  const myCard = { source: 'project-person', issuer_kind: 'project-person', issuer_ref: 'work:project:q', grantor_subject: 'person-2' }
  assert.equal(linkedControlOpenTarget({ ...myCard, grantor_subject: '' }, { control_id: 'c', issuer_ref: 'work:project:q', issuer_kind: 'project' }), null)
  assert.equal(linkedControlOpenTarget(myCard, { control_id: 'c', issuer_ref: '', issuer_kind: 'project' }), null)
  assert.equal(linkedControlOpenTarget(myCard, { control_id: 'c', issuer_ref: 'problem-board@1-0:project:q', issuer_kind: 'project' }), null)
  // issuer_kind "project" on a Card that is not a My Card: unchanged, control_card_get.
  const agentCard = { source: 'grant', issuer_kind: 'kdcube_agent_descriptor', issuer_ref: 'agent:x', grantor_subject: 'person-2' }
  assert.deepEqual(controlCardGetRequest(linkedControlOpenTarget(agentCard, { control_id: 'control-project', issuer_ref: 'work:project:q', issuer_kind: 'project' })),
    { operation: 'control_card_get', data: { control_id: 'control-project' } })
  const plain = { source: 'grant', issuer_kind: 'application', issuer_ref: 'app', grantor_subject: 'person-3' }
  assert.deepEqual(linkedControlOpenTarget(plain, { control_id: 'control-own', issuer_ref: 'app', issuer_kind: 'application' }), { controlId: 'control-own' })
  assert.equal(linkedControlOpenTarget(plain, { control_id: '  ', issuer_ref: 'x' }), null)
})

test('the panel opens the linked Card through the helper and refuses a mismatch', () => {
  const panel = readFileSync(new URL('../src/features/delegatedAccess/DelegatedAccessPanel.tsx', import.meta.url), 'utf8')
  assert.match(panel, /onClick=\{\(\) => void openLinkedControlCard\(item, binding\)\}/)
  assert.match(panel, /const target = linkedControlOpenTarget\(item, binding\);/)
  assert.match(panel, /if \(!result\.access \|\| !isLinkedControlCard\(result\.access, target\)\) \{/)
  assert.doesNotMatch(panel, /loadControlCard\(\{ controlId: cleanControlId \}\)/)
})

test('a My Card names its project Control Card "Control Card", never an account UUID', () => {
  const panel = readFileSync(new URL('../src/features/delegatedAccess/DelegatedAccessPanel.tsx', import.meta.url), 'utf8')
  assert.match(panel, /const label = linkedControlOpenTarget\(item, binding\)\?\.targetSubject\s*\? 'Control Card'\s*: controlIssuerLabel\(binding, issuerViewer\);/)
  // The head then reads "Card composed with Control Card (AND)".
  assert.match(panel, /`Card composed with \$\{label\} \(\$\{mode\}\)`/)
})
