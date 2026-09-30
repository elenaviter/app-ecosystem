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
  const myCard = {
    source: 'project-person', issuer_kind: 'project-person', issuer_ref: 'work:project:quickstart',
    grantor_subject: 'platform-user-2', access_id: 'my-card-access-id',
  }
  const binding = { control_id: 'control-person', issuer_ref: 'work:project:quickstart', issuer_kind: 'project', issuer_label: 'c1a2b3-uuid-label' }
  const target = linkedControlOpenTarget(myCard, binding)
  assert.deepEqual(target, { controlId: 'control-person', projectRef: 'work:project:quickstart', targetSubject: 'platform-user-2' })
  assert.equal(controlCardGetRequest(target).operation, 'project_person_control_get')
  // The key is the binding's control_id: never the label, never My Card's access_id.
  assert.equal(isLinkedControlCard({ access_id: 'control-person' }, target), true)
  assert.equal(isLinkedControlCard({ access_id: 'my-card-access-id' }, target), false)
  assert.equal(isLinkedControlCard({ access_id: 'c1a2b3-uuid-label' }, target), false)
  assert.equal(isLinkedControlCard(null, target), false)
})

test('an agent Card capped by the project Control Card reads it through the project', () => {
  const agentCard = { source: 'grant', issuer_kind: 'kdcube_agent_descriptor', issuer_ref: 'agent:x', grantor_subject: 'owner-1' }
  const target = linkedControlOpenTarget(agentCard, { control_id: 'control-project', issuer_ref: 'work:project:quickstart', issuer_kind: 'project' })
  assert.deepEqual(target, { controlId: 'control-project', projectRef: 'work:project:quickstart' })
  assert.equal(controlCardGetRequest(target).operation, 'project_control_card_get')
})

test('only a My Card bound by the project takes the person route, and it fails closed without coordinates', () => {
  // issuer_kind "project" alone is not enough: an agent Card of the viewer's
  // own is not a My Card, so it reads the project's Control Card.
  const ownAgentCard = { source: 'grant', issuer_kind: 'kdcube_agent_descriptor', issuer_ref: 'agent:y', grantor_subject: 'platform-user-2' }
  assert.equal(controlCardGetRequest(linkedControlOpenTarget(ownAgentCard, {
    control_id: 'control-project', issuer_ref: 'work:project:q', issuer_kind: 'project',
  })).operation, 'project_control_card_get')
  const myCard = { source: 'project-person', issuer_kind: 'project-person', issuer_ref: 'work:project:q', grantor_subject: '' }
  assert.equal(linkedControlOpenTarget(myCard, { control_id: 'control-person', issuer_ref: 'work:project:q', issuer_kind: 'project' }), null)
  assert.equal(linkedControlOpenTarget({ ...myCard, grantor_subject: 'platform-user-2' }, { control_id: 'control-person', issuer_ref: '', issuer_kind: 'project' }), null)
  // A plain link stays the viewer's own Card; no id opens nothing.
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
})
