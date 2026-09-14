import assert from 'node:assert/strict'
import test from 'node:test'

import { composeControlCardAuthority } from '../src/features/delegatedAccess/controlCardPreview.ts'

const RESOURCE = 'https://example.test/mcp'

test('pending AND preview uses the raw control authority, not the saved intersection', () => {
  const pendingCaller = {
    resource_grants: { [RESOURCE]: ['work:observe', 'work:relay'] },
    resource_operations: { [RESOURCE]: ['project.plan.item', 'project.plan.search', 'project.plan.publish'] },
    effective_named_service_operations: {
      [RESOURCE]: { work: ['project.plan.item', 'project.plan.search', 'project.plan.publish'] },
    },
    account_scope: { github: { '*': ['repo:read', 'repo:write'] } },
  }
  const rawControl = {
    resource_grants: { [RESOURCE]: ['work:observe', 'work:relay'] },
    resource_operations: { [RESOURCE]: ['project.plan.item', 'project.plan.search'] },
    effective_named_service_operations: {
      [RESOURCE]: { work: ['project.plan.item', 'project.plan.search'] },
    },
    account_scope: { github: { account_a: ['repo:read'] } },
  }

  assert.deepEqual(composeControlCardAuthority(pendingCaller, rawControl, 'and'), {
    operations: [],
    resource_grants: { [RESOURCE]: ['work:observe', 'work:relay'] },
    resource_operations: { [RESOURCE]: ['project.plan.item', 'project.plan.search'] },
    named_service_operations: {
      [RESOURCE]: { work: ['project.plan.item', 'project.plan.search'] },
    },
    effective_named_service_operations: {
      [RESOURCE]: { work: ['project.plan.item', 'project.plan.search'] },
    },
    account_scope: { github: { account_a: ['repo:read'] } },
  })
})

test('OR preview unions each authority family and preserves wildcard claims', () => {
  const caller = {
    resource_grants: { [RESOURCE]: ['work:observe'] },
    resource_operations: { [RESOURCE]: ['project.plan.item'] },
    effective_named_service_operations: { [RESOURCE]: { work: ['project.plan.item'] } },
    account_scope: { github: { account_a: ['repo:read'] } },
  }
  const control = {
    resource_grants: { [RESOURCE]: ['*'] },
    resource_operations: { [RESOURCE]: ['project.plan.search'] },
    effective_named_service_operations: { [RESOURCE]: { work: ['project.plan.search'] } },
    account_scope: { github: { account_a: ['*'] } },
  }

  const effective = composeControlCardAuthority(caller, control, 'or')

  assert.deepEqual(effective.resource_grants, { [RESOURCE]: ['*'] })
  assert.deepEqual(effective.resource_operations, {
    [RESOURCE]: ['project.plan.item', 'project.plan.search'],
  })
  assert.deepEqual(effective.effective_named_service_operations, {
    [RESOURCE]: { work: ['project.plan.item', 'project.plan.search'] },
  })
  assert.deepEqual(effective.account_scope, { github: { account_a: ['*'] } })
})
