import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { applicationApiInventory } from '../src/features/delegatedAccess/applicationApiInventory.ts'

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
        },
        {
          alias: 'agent_capabilities',
          method: 'POST',
          route: 'operations',
          userTypes: ['registered', 'paid'],
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
  assert.match(catalog, /api\.alias/)
  assert.match(catalog, /api\.method/)

  const panel = source('src/features/delegatedAccess/DelegatedAccessPanel.tsx')
  assert.match(panel, />Service permissions</)
  assert.equal((panel.match(/\{resource === '\*' \? <ApplicationApiCatalog/g) || []).length, 1)
  assert.equal((panel.match(/\{item\.resource === '\*' \? <ApplicationApiCatalog/g) || []).length, 1)
})
