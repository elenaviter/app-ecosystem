import assert from 'node:assert/strict'
import test from 'node:test'

import { changedEditorCardProperties } from '../src/features/delegatedAccess/cardEditorProperties.ts'
import {
  AGENT_CAPABILITY_DEFAULTS_PROPERTY,
  AGENT_CAPABILITY_SELECTION_PROPERTY,
  agentCapabilitySelectionMap,
  agentCapabilitySelectionProperty,
  cardAgentCapabilityAuthority,
  cardAgentCapabilitySelection,
} from '../src/features/delegatedAccess/agentCapabilitySelection.ts'

const resource = 'urn:kdcube:agent:synthetic-app:assistant'
const authority = cardAgentCapabilityAuthority({
  'kdcube.agent_capability_authority': agentCapabilitySelectionProperty(resource, {
    models: ['model-a', 'model-b'], named_service_operations: ['slack/object.list'],
  }),
})
const stored = {
  marker: { revision: 'descriptor-1' },
  [AGENT_CAPABILITY_SELECTION_PROPERTY]: agentCapabilitySelectionProperty(resource, {
    models: ['model-a'], named_service_operations: ['slack/object.list'],
  }),
}
const capabilities = agentCapabilitySelectionMap(cardAgentCapabilitySelection(stored))
const args = {
  selected: stored, seeded: stored,
  selectedCapabilities: capabilities, seededCapabilities: capabilities,
  capabilityAuthority: authority, descriptorAuthority: null,
}

test('a hosted-agent permission-only Save omits rebuilt properties, including its capability projection', () => {
  // Permission choices travel in resource/named-service fields. The editor
  // may normalize a legacy empty capability group while rebuilding properties.
  const legacy = { ...stored, [AGENT_CAPABILITY_SELECTION_PROPERTY]: {
    ...stored[AGENT_CAPABILITY_SELECTION_PROPERTY], capabilities: { ...capabilities, tools: [] },
  } }
  assert.equal(changedEditorCardProperties({ ...args, selected: legacy, seeded: legacy }), undefined)
})

test('an untouched unsorted selection with escaped ids is compared through the same encoder', () => {
  const unsorted = { ...stored, [AGENT_CAPABILITY_SELECTION_PROPERTY]: {
    schema: 'connection_hub.agent_capability_policy.v1', resource,
    capabilities: { tools: ['tool.z%2Fread', 'tool.a%20write'], models: ['model-a'], empty: [] },
  } }
  const seeded = agentCapabilitySelectionMap(cardAgentCapabilitySelection(unsorted))
  assert.equal(changedEditorCardProperties({ ...args, selected: unsorted, seeded: unsorted,
    selectedCapabilities: seeded, seededCapabilities: seeded }), undefined)
})

test('an explicit hosted-agent capability change still travels for backend validation', () => {
  const result = changedEditorCardProperties({
    ...args, selectedCapabilities: { ...capabilities, models: ['model-b'] },
  })
  assert.deepEqual(result[AGENT_CAPABILITY_SELECTION_PROPERTY].capabilities.models, ['model-b'])
  assert.deepEqual(result.marker, stored.marker)
})

test('descriptor Control defaults use the same complete seed comparison', () => {
  const defaults = { [AGENT_CAPABILITY_DEFAULTS_PROPERTY]: agentCapabilitySelectionProperty(resource, capabilities) }
  const controlArgs = { ...args, selected: defaults, seeded: defaults,
    capabilityAuthority: null, descriptorAuthority: authority }
  assert.equal(changedEditorCardProperties(controlArgs), undefined)
  const changed = changedEditorCardProperties({ ...controlArgs,
    selectedCapabilities: { ...capabilities, models: ['model-b'] } })
  assert.deepEqual(changed[AGENT_CAPABILITY_DEFAULTS_PROPERTY].capabilities.models, ['model-b'])
  assert.equal(AGENT_CAPABILITY_SELECTION_PROPERTY in changed, false)
})

test('explicit ordinary property changes are retained for server validation and refusal', () => {
  const ordinary = { ...args, capabilityAuthority: null, selected: { label_choice: 'new' },
    seeded: { label_choice: 'old' } }
  assert.deepEqual(changedEditorCardProperties(ordinary), { label_choice: 'new' })
  assert.equal(changedEditorCardProperties({ ...ordinary, selected: ordinary.seeded }), undefined)
})
