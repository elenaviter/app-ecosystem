import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  AGENT_CAPABILITY_AUTHORITY_PROPERTY,
  AGENT_CAPABILITY_DEFAULTS_PROPERTY,
  AGENT_CAPABILITY_METADATA_PROPERTY,
  AGENT_CAPABILITY_SELECTION_PROPERTY,
  AGENT_CAPABILITY_POLICY_SCHEMA,
  AGENT_CAPABILITY_METADATA_SCHEMA,
  AGENT_DESCRIPTOR_CONTROL_PROPERTY,
  AGENT_DESCRIPTOR_CONTROL_SCHEMA,
  cardAgentCapabilityAuthority,
  cardAgentCapabilityDefaults,
  cardAgentCapabilityMetadata,
  cardAgentCapabilitySelection,
  cardAgentDescriptorTarget,
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

test('model and instruction defaults retain their Card vocabulary', () => {
  const parsed = cardAgentCapabilitySelection(selection({
    models: ['anthropic/claude-sonnet-4-6'],
    instruction_profiles: ['full'],
  }))

  assert.deepEqual(parsed?.groups.map(({ category, label }) => ({ category, label })), [
    { category: 'models', label: 'Models' },
    { category: 'instruction_profiles', label: 'Instruction profiles' },
  ])
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
    [AGENT_CAPABILITY_DEFAULTS_PROPERTY]: {
      schema: AGENT_CAPABILITY_POLICY_SCHEMA,
      resource: RESOURCE,
      capabilities: { tools: ['web/search'] },
    },
    [AGENT_DESCRIPTOR_CONTROL_PROPERTY]: {
      schema: AGENT_DESCRIPTOR_CONTROL_SCHEMA,
      resource: RESOURCE,
    },
  }

  assert.equal(cardAgentCapabilityAuthority(properties)?.selectedCount, 1)
  assert.equal(cardAgentCapabilityDefaults(properties)?.selectedCount, 1)
  assert.deepEqual(cardAgentCapabilityMetadata(properties), {
    tools: {
      'web/search': { title: 'Search', description: 'Search project records.' },
    },
  })
  assert.equal(isAgentDescriptorControl(properties), true)
  assert.deepEqual(cardAgentDescriptorTarget(properties), {
    application: 'problem-board@1-0',
    agent: 'main',
  })
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

test('Agent Cards use the ordinary Card workbench plus KDCube metadata', () => {
  const panel = readFileSync(
    new URL('../src/features/delegatedAccess/DelegatedAccessPanel.tsx', import.meta.url),
    'utf8',
  )

  assert.match(panel, /title="KDCube agent defaults"/)
  assert.match(panel, /title="KDCube administrator preset"/)
  assert.match(panel, /renderCardComposition\(record, \{ editing: true \}\)/)
  assert.match(panel, /renderEditResourceSections\(record, effectiveComposition\)/)
  assert.match(panel, /renderAccountScopePicker\(/)
  assert.match(panel, /updateDelegatedAccess\(\{/)
  assert.match(panel, /AGENT_CAPABILITY_SELECTION_PROPERTY/)
  assert.match(panel, /AGENT_CAPABILITY_DEFAULTS_PROPERTY/)
  assert.match(panel, /const capabilityControl = item\.control_card;/)
  assert.match(panel, /descriptor initialize this preset/)
  assert.match(panel, /descriptorCapabilityDefaults/)
  assert.match(panel, /title="KDCube administrator preset"[\s\S]*editable[\s\S]*singleChoiceCategories/)
  assert.match(panel, /mergeBundleProps\(descriptorTarget\.application/)
  assert.match(panel, /agent_capability_control_overrides/)
  assert.match(panel, /Reset to Control defaults/)
  assert.match(panel, /resetToControlDefaults: true/)
  assert.match(panel, /Connected accounts and custom MCP servers stay on this card/)
  assert.match(panel, /if \(isAgentCapabilityCard\(item\)\) return null/)
  assert.doesNotMatch(panel, /saveAgentCapabilityBase/)
  assert.doesNotMatch(panel, /specializedCapabilityCard/)

  const categoryStart = panel.indexOf('const KDCUBE_AGENT_CARD_CATEGORIES')
  const categoryEnd = panel.indexOf('];', categoryStart)
  const cardCategories = panel.slice(categoryStart, categoryEnd)
  assert.doesNotMatch(cardCategories, /mcp_servers|mcp_tools/)
})

test('the capability editor groups child entries under their owning capability', () => {
  const view = readFileSync(
    new URL('../src/features/delegatedAccess/AgentCapabilityPolicyView.tsx', import.meta.url),
    'utf8',
  )

  assert.match(view, /parentCategory: 'tool_groups',[\s\S]*childCategory: 'tools'/)
  assert.match(view, /parentCategory: 'mcp_servers',[\s\S]*childCategory: 'mcp_tools'/)
  assert.match(view, /parentCategory: 'named_services',[\s\S]*childCategory: 'named_service_operations'/)
  assert.match(view, /parentCategory: 'resources',[\s\S]*childCategory: 'resource_operations'/)
  assert.match(view, /agent-capability-policy__branch/)
  assert.match(view, /entry\?\.description \? <small>\{entry\.description\}<\/small>/)
  assert.match(view, /type=\{singleChoice \? 'radio' : 'checkbox'\}/)
  assert.match(view, /withSingleChoice\(selection, category, capability\)/)
})
