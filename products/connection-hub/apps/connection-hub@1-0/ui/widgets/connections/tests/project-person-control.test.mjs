import assert from 'node:assert/strict'
import test from 'node:test'

import {
  controlCardGetRequest,
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
