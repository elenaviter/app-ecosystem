import assert from 'node:assert/strict'
import test from 'node:test'

import {
  AGENT_CAPABILITY_AUTHORITY_PROPERTY,
  AGENT_CAPABILITY_POLICY_SCHEMA,
  AGENT_DESCRIPTOR_CONTROL_PROPERTY,
  AGENT_DESCRIPTOR_CONTROL_SCHEMA,
  descriptorOpenActions,
} from '../src/features/delegatedAccess/agentCapabilitySelection.ts'

const RESOURCE = 'urn:kdcube:app:t:p:workspace@1:main'

function control(operations, provenance = {}) {
  return {
    source: 'control',
    provenance,
    properties: {
      [AGENT_DESCRIPTOR_CONTROL_PROPERTY]: { schema: AGENT_DESCRIPTOR_CONTROL_SCHEMA, resource: RESOURCE, controlled_dimensions: [] },
      [AGENT_CAPABILITY_AUTHORITY_PROPERTY]: {
        schema: AGENT_CAPABILITY_POLICY_SCHEMA,
        resource: RESOURCE,
        capabilities: { named_service_operations: operations },
      },
    },
  }
}

test('W38: a descriptor Control names the services whose descriptor allows every action', () => {
  const open = descriptorOpenActions(control(['docs/object.action', 'docs/object.action.copy', 'slides/object.action.add_slide', 'sheets/object.list']))
  assert.deepEqual(open, { namespaces: ['docs'], overridden: false })
})

test('W38: an administrator edit of the Control is reported as an explicit list in force', () => {
  const open = descriptorOpenActions(control(['docs/object.action', 'docs/object.action.copy'], { agent_descriptor_card_edited: true }))
  assert.deepEqual(open, { namespaces: ['docs'], overridden: true })
})

test('W38: no notice for a list-only descriptor, a caller Card or a non-descriptor Control', () => {
  assert.equal(descriptorOpenActions(control(['work/object.action.project.register'])), null)
  assert.equal(descriptorOpenActions({ ...control(['docs/object.action']), source: 'agent' }), null)
  assert.equal(descriptorOpenActions({ source: 'control', properties: {} }), null)
})
