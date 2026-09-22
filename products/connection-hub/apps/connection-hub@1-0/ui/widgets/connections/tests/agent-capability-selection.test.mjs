import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  AGENT_CAPABILITY_AUTHORITY_PROPERTY,
  AGENT_CAPABILITY_METADATA_PROPERTY,
  AGENT_CAPABILITY_SELECTION_PROPERTY,
  AGENT_CAPABILITY_POLICY_SCHEMA,
  AGENT_CAPABILITY_METADATA_SCHEMA,
  AGENT_DESCRIPTOR_CONTROL_PROPERTY,
  AGENT_DESCRIPTOR_CONTROL_SCHEMA,
  cardAgentCapabilityAuthority,
  cardAgentCapabilityMetadata,
  cardAgentCapabilitySelection,
  isAgentDescriptorControl,
} from '../src/features/delegatedAccess/agentCapabilitySelection.ts'

const RESOURCE = 'urn:kdcube:app:demo-tenant:demo-project:problem-board@1-0:main'

function selection(capabilities) {
  return {
    [AGENT_CAPABILITY_SELECTION_PROPERTY]: {
      schema: AGENT_CAPABILITY_POLICY_SCHEMA,
      resource: RESOURCE,
      capabilities,
    },
  }
}

test('an Agent Card base names the selected task and its operations', () => {
  const parsed = cardAgentCapabilitySelection(selection({
    named_services: ['task'],
    named_service_operations: [
      'task/object.action.assign',
      'task/object.action.cancel',
      'task/object.action.complete',
      'task/object.action.start',
      'task/object.get',
      'task/object.list',
      'task/object.search',
    ],
  }))

  assert.equal(parsed?.resource, RESOURCE)
  assert.equal(parsed?.selectedCount, 8)
  assert.deepEqual(parsed?.groups.map(({ category, label }) => ({ category, label })), [
    { category: 'named_services', label: 'Named services' },
    { category: 'named_service_operations', label: 'Service operations' },
  ])
  assert.deepEqual(parsed?.groups[0].values, ['task'])
})

test('a cleared Agent Card base remains a valid named empty selection', () => {
  const parsed = cardAgentCapabilitySelection(selection({
    named_services: [],
    named_service_operations: [],
  }))

  assert.ok(parsed)
  assert.equal(parsed.selectedCount, 0)
  assert.deepEqual(parsed.groups, [])
})

test('malformed properties are not presented as capability authority', () => {
  assert.equal(cardAgentCapabilitySelection({
    [AGENT_CAPABILITY_SELECTION_PROPERTY]: {
      schema: 'unknown',
      resource: RESOURCE,
      capabilities: { named_services: ['task'] },
    },
  }), null)
  assert.equal(cardAgentCapabilitySelection(selection({ named_services: '*' })), null)
})

test('the descriptor Control Card exposes its ceiling and presentation metadata', () => {
  const properties = {
    [AGENT_CAPABILITY_AUTHORITY_PROPERTY]: {
      schema: AGENT_CAPABILITY_POLICY_SCHEMA,
      resource: RESOURCE,
      capabilities: { tools: ['web/search'] },
    },
    [AGENT_CAPABILITY_METADATA_PROPERTY]: {
      schema: AGENT_CAPABILITY_METADATA_SCHEMA,
      resource: RESOURCE,
      entries: {
        tools: {
          'web/search': { title: 'Search', description: 'Search project records.' },
        },
      },
    },
    [AGENT_DESCRIPTOR_CONTROL_PROPERTY]: {
      schema: AGENT_DESCRIPTOR_CONTROL_SCHEMA,
      resource: RESOURCE,
    },
  }

  assert.equal(cardAgentCapabilityAuthority(properties)?.selectedCount, 1)
  assert.deepEqual(cardAgentCapabilityMetadata(properties), {
    tools: {
      'web/search': { title: 'Search', description: 'Search project records.' },
    },
  })
  assert.equal(isAgentDescriptorControl(properties), true)
})

test('the Card surface renders the property as an Agent capability base', () => {
  const panel = readFileSync(
    new URL('../src/features/delegatedAccess/DelegatedAccessPanel.tsx', import.meta.url),
    'utf8',
  )
  assert.match(panel, /label="Agent capability base"/)
  assert.match(panel, /Starting selection for new conversations\./)
  assert.match(panel, /New conversations start with an empty capability base\./)
  assert.match(panel, /capabilityBase \? null/)
})

test('resident and descriptor capability Cards use their dedicated workbench paths', () => {
  const panel = readFileSync(
    new URL('../src/features/delegatedAccess/DelegatedAccessPanel.tsx', import.meta.url),
    'utf8',
  )
  const slice = readFileSync(
    new URL('../src/features/delegatedAccess/delegatedAccessSlice.ts', import.meta.url),
    'utf8',
  )

  assert.match(panel, /title="Starting capabilities for new conversations"/)
  assert.match(panel, /title="Descriptor capability ceiling"/)
  assert.match(panel, /saveAgentCapabilityBase/)
  assert.match(panel, /descriptorCapabilityControl \? null/)
  assert.match(slice, /selected_capabilities: selectedCapabilities/)
})
