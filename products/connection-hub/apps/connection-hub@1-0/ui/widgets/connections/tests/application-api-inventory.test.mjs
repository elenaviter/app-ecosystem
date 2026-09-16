import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  APPLICATION_OPERATION_POLICY_PROPERTY,
  applicationApiInventory,
  applicationOperationPolicyEnabled,
  filterApplicationApiInventory,
  withApplicationOperationPolicy,
} from '../src/features/delegatedAccess/applicationApiInventory.ts'

const source = (relativePath) =>
  readFileSync(new URL(`../${relativePath}`, import.meta.url), 'utf8')

test('bundle catalog becomes a sorted per-app API inventory without dropping empty apps', () => {
  const inventory = applicationApiInventory({
    available_bundles: {
      'task-and-memo-app@1-0': {
        id: 'task-and-memo-app@1-0',
        name: 'Tasks and memos',
        apis: [
          {
            alias: 'agent_capabilities',
            http_method: 'post',
            route: 'operations',
            user_types: ['registered', 'paid', 'registered'],
            roles: ['kdcube:role:member'],
            operation_id: 'agent.capabilities',
            operation_ref: 'urn:kdcube:application-operation:task-and-memo-app%401-0:agent.capabilities',
            operation_id_explicit: true,
          },
          {
            alias: 'account_status',
            http_method: 'GET',
            route: 'public',
            user_types: [],
          },
        ],
      },
      'quiet-app@1-0': {
        name: 'Quiet app',
        description: 'Declares no API surface.',
      },
    },
  })

  assert.deepEqual(inventory, [
    {
      id: 'quiet-app@1-0',
      label: 'Quiet app',
      description: 'Declares no API surface.',
      apis: [],
    },
    {
      id: 'task-and-memo-app@1-0',
      label: 'Tasks and memos',
      description: '',
      apis: [
        {
          alias: 'account_status',
          method: 'GET',
          route: 'public',
          userTypes: [],
          roles: [],
          operationId: '',
          operationRef: '',
          operationIdExplicit: false,
        },
        {
          alias: 'agent_capabilities',
          method: 'POST',
          route: 'operations',
          userTypes: ['registered', 'paid'],
          roles: ['kdcube:role:member'],
          operationId: 'agent.capabilities',
          operationRef: 'urn:kdcube:application-operation:task-and-memo-app%401-0:agent.capabilities',
          operationIdExplicit: true,
        },
      ],
    },
  ])
})

test('all-services API inventory uses the existing platform endpoint and leaves role selection in place', () => {
  const client = source('src/api/client.ts')
  assert.match(client, /\/api\/integrations\/bundles`/)
  assert.match(client, /searchParams\.set\('tenant', settings\.getTenant\(\)\)/)
  assert.match(client, /searchParams\.set\('project', settings\.getProject\(\)\)/)

  const catalog = source('src/features/delegatedAccess/ApplicationApiCatalog.tsx')
  assert.match(catalog, /No APIs declared/)
  assert.match(catalog, /<dt>Alias<\/dt>/)
  assert.match(catalog, /<dt>Method<\/dt>/)
  assert.match(catalog, /<dt>Route<\/dt>/)
  assert.match(catalog, /<dt>User types<\/dt>/)
  assert.match(catalog, /<dt>Roles<\/dt>/)
  assert.match(catalog, /selectedOperations/)
  assert.match(catalog, /onOperationChange/)
  assert.match(catalog, /Search applications and APIs/)
  assert.match(catalog, /Select displayed APIs for/)
  assert.match(catalog, /filtered \? 'All shown' : 'All'/)
  assert.match(catalog, /filtered \? 'None shown' : 'None'/)
  assert.match(catalog, /selectedCount.*operationRefs\.length/s)
  assert.match(catalog, /No longer in the active catalog/)
  assert.match(catalog, /api\.operationRef/)
  assert.match(catalog, /api\.alias/)
  assert.match(catalog, /api\.method/)

  const panel = source('src/features/delegatedAccess/DelegatedAccessPanel.tsx')
  assert.match(panel, />Service permissions</)
  assert.match(panel, /selectedOperations=\{resourceOperations\[APPLICATION_API_RESOURCE\]/)
  assert.match(panel, /selectedOperations=\{editResourceOperations\[APPLICATION_API_RESOURCE\]/)
  assert.match(panel, /createApplicationRoleMissing/)
})

test('API search keeps matching apps whole and narrows other apps to matching operations', () => {
  const applications = [
    {
      id: 'problem-board@1-0',
      label: 'Problem Board',
      description: 'Coordinate project work.',
      apis: [
        {
          alias: 'problem_board',
          method: 'POST',
          route: 'operations',
          userTypes: ['registered'],
          roles: [],
          operationId: 'api.operations.post.problem_board',
          operationRef: 'urn:problem-board',
          operationIdExplicit: false,
        },
      ],
    },
    {
      id: 'kdcube-services@1-0',
      label: 'KDCube services',
      description: 'Shared user services.',
      apis: [
        {
          alias: 'telegram_send',
          method: 'POST',
          route: 'operations',
          userTypes: ['registered'],
          roles: [],
          operationId: 'api.operations.post.telegram_send',
          operationRef: 'urn:telegram-send',
          operationIdExplicit: false,
        },
        {
          alias: 'unrelated',
          method: 'GET',
          route: 'operations',
          userTypes: ['registered'],
          roles: [],
          operationId: 'api.operations.get.unrelated',
          operationRef: 'urn:unrelated',
          operationIdExplicit: false,
        },
      ],
    },
  ]

  assert.equal(filterApplicationApiInventory(applications, 'problem board')[0].apis.length, 1)
  assert.deepEqual(
    filterApplicationApiInventory(applications, 'telegram')[0].apis.map((api) => api.alias),
    ['telegram_send'],
  )
  assert.equal(filterApplicationApiInventory(applications, 'missing').length, 0)
})

test('a reviewed application selection has an explicit marker and preserves other Card properties', () => {
  const properties = withApplicationOperationPolicy({
    coordination: { version_control: { model: 'shared-main' } },
  })

  assert.equal(applicationOperationPolicyEnabled(properties), true)
  assert.deepEqual(properties, {
    coordination: { version_control: { model: 'shared-main' } },
    [APPLICATION_OPERATION_POLICY_PROPERTY]: {
      schema: 'kdcube.application_operations.v1',
      mode: 'selected',
    },
  })
  assert.equal(applicationOperationPolicyEnabled({}), false)
  assert.equal(applicationOperationPolicyEnabled({
    [APPLICATION_OPERATION_POLICY_PROPERTY]: {
      schema: 'kdcube.application_operations.v1',
      mode: 'unexpected',
    },
  }), false)
})

test('create, OAuth consent, and edit persist the reviewed application-operation policy', () => {
  const panel = source('src/features/delegatedAccess/DelegatedAccessPanel.tsx')

  assert.match(panel, /properties: applicationOperationProperties/)
  assert.match(panel, /applicationOperationPolicyEnabled\(item\.properties\)/)
  assert.match(panel, /setEditApplicationOperationPolicyEnabled\(true\)/)
  assert.match(panel, /withApplicationOperationPolicy\(item\.properties\)/)
})
