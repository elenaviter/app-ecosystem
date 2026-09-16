import assert from 'node:assert/strict'
import test from 'node:test'

import {
  authorityAllowsOuterOperation,
  authorityHasAccess,
  authorityOuterOperationCount,
  authorityResourceKeys,
  compositionRowState,
  composeControlCardAuthority,
  controlSnapshotIsExact,
  outerOperationsExcludedByControl,
} from '../src/features/delegatedAccess/controlCardPreview.ts'

const RESOURCE = 'https://example.test/mcp'
const SNAPSHOT = {
  schema: 'connection_hub.control_snapshot.v1',
  mode: 'exact',
  state: 'exact',
  basis_catalog_version: 'catalog-v1',
}

function exactControl(authority) {
  return {
    named_service_operations: {},
    account_scope: {},
    properties: { 'connection_hub.control_snapshot': SNAPSHOT },
    ...authority,
  }
}

test('pending AND preview uses the raw control authority, not the saved intersection', () => {
  const pendingCaller = {
    resource_grants: { [RESOURCE]: ['work:observe', 'work:relay'] },
    resource_operations: { [RESOURCE]: ['project.plan.item', 'project.plan.search', 'project.plan.publish'] },
    effective_named_service_operations: {
      [RESOURCE]: { work: ['project.plan.item', 'project.plan.search', 'project.plan.publish'] },
    },
    account_scope: { github: { '*': ['repo:read', 'repo:write'] } },
  }
  const rawControl = exactControl({
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
    resource_grants: { [RESOURCE]: ['*'] },
    resource_operations: { [RESOURCE]: ['project.plan.item'] },
    effective_named_service_operations: { [RESOURCE]: { work: ['project.plan.item'] } },
    account_scope: { github: { account_a: ['*'] } },
  }
  const control = exactControl({
    resource_grants: { [RESOURCE]: ['work:relay'] },
    resource_operations: { [RESOURCE]: ['project.plan.search'] },
    named_service_operations: { [RESOURCE]: { work: ['project.plan.search'] } },
    effective_named_service_operations: { [RESOURCE]: { work: ['project.plan.search'] } },
    account_scope: { github: { account_a: ['repo:write'] } },
  })

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

test('claimless effective tools remain visible authority', () => {
  const authority = {
    operations: [],
    resource_grants: { [RESOURCE]: [] },
    resource_operations: { [RESOURCE]: ['project.plan.import', 'project.plan.publish'] },
  }

  assert.equal(authorityHasAccess(authority), true)
  assert.equal(authorityOuterOperationCount(authority), 2)
  assert.deepEqual(authorityResourceKeys(authority), [RESOURCE])
})

test('AND control identifies selected caller tools that remain inactive', () => {
  const caller = {
    resource_grants: { [RESOURCE]: [] },
    resource_operations: {
      [RESOURCE]: ['project.plan.import', 'project.plan.search', 'project.plan.publish'],
    },
  }
  const control = exactControl({
    resource_grants: { [RESOURCE]: [] },
    resource_operations: { [RESOURCE]: ['project.plan.search', 'project.plan.publish'] },
  })

  assert.equal(authorityAllowsOuterOperation(control, RESOURCE, 'project.plan.search'), true)
  assert.equal(authorityAllowsOuterOperation(control, RESOURCE, 'project.plan.import'), false)
  assert.deepEqual(outerOperationsExcludedByControl(caller, control), [
    { resource: RESOURCE, operation: 'project.plan.import' },
  ])
})

test('a wildcard Control Card fails closed in preview just as it does at runtime', () => {
  const control = exactControl({ resource_operations: { [RESOURCE]: ['*'] } })
  assert.equal(controlSnapshotIsExact(control), false)
  assert.equal(authorityAllowsOuterOperation(control, RESOURCE, 'project.plan.import'), false)
  assert.deepEqual(composeControlCardAuthority({}, control, 'or'), {
    operations: [],
    resource_grants: {},
    resource_operations: {},
    named_service_operations: {},
    effective_named_service_operations: {},
    account_scope: {},
  })
})

test('an incomplete snapshot marker fails closed in preview', () => {
  const missingBasis = exactControl({
    properties: {
      'connection_hub.control_snapshot': {
        schema: SNAPSHOT.schema,
        mode: SNAPSHOT.mode,
        state: SNAPSHOT.state,
      },
    },
  })
  const unknownState = exactControl({
    properties: {
      'connection_hub.control_snapshot': {
        ...SNAPSHOT,
        state: 'unknown',
      },
    },
  })

  assert.equal(controlSnapshotIsExact(missingBasis), false)
  assert.equal(controlSnapshotIsExact(unknownState), false)
})

test('Effective Card row states expose AND removals and OR additions', () => {
  assert.equal(compositionRowState(true, true, 'and'), 'normal')
  assert.equal(compositionRowState(true, false, 'and'), 'removed')
  assert.equal(compositionRowState(false, true, 'and'), 'absent')
  assert.equal(compositionRowState(true, false, 'or'), 'normal')
  assert.equal(compositionRowState(false, true, 'or'), 'added')
  assert.equal(compositionRowState(false, false, 'or'), 'absent')
})
