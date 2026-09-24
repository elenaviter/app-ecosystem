import assert from 'node:assert/strict'
import test from 'node:test'

import {
  projectPersonControlCoordinates,
} from '../src/features/delegatedAccess/projectPersonControl.ts'

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
