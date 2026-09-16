import assert from 'node:assert/strict'
import test from 'node:test'

import {
  APPLICATION_OPERATION_POLICY_PROPERTY,
  applicationOperationPropertiesForSelection,
  applicationOperationPolicyEnabled,
  applicationOperationRoleFor,
  applicationOperationRoleIsElevated,
  applicationOperationRolePolicy,
  availableApplicationRoles,
  composeApplicationOperationRolePolicies,
  delegableApplicationRoles,
  platformRoleAllowedBy,
  projectedRoleAllows,
  seedApplicationOperationRolePolicy,
  unavailableApplicationPolicyRoles,
  unselectedApplicationOperationOverrides,
  withApplicationOperationPolicy,
} from '../src/features/delegatedAccess/applicationOperationRoles.ts'

const REGISTERED = 'kdcube:role:registered'
const PAID = 'kdcube:role:paid'
const PRIVILEGED = 'kdcube:role:privileged'
const SUPER_ADMIN = 'kdcube:role:super-admin'

test('V1 policies migrate in memory from the strongest stored application role', () => {
  const properties = {
    [APPLICATION_OPERATION_POLICY_PROPERTY]: {
      schema: 'kdcube.application_operations.v1',
      mode: 'selected',
    },
  }

  assert.deepEqual(applicationOperationRolePolicy(properties, {
    '*': [REGISTERED, SUPER_ADMIN, PAID],
  }), {
    defaultRole: SUPER_ADMIN,
    operationRoles: {},
  })
  assert.equal(applicationOperationPolicyEnabled(properties), true)
  assert.deepEqual(seedApplicationOperationRolePolicy({}, { '*': [PAID] }), {
    defaultRole: PAID,
    operationRoles: {},
  })
})

test('V2 writes one default and only selected non-default overrides', () => {
  const properties = withApplicationOperationPolicy({ keep: true }, {
    defaultRole: REGISTERED,
    operationRoles: {
      'urn:default': REGISTERED,
      'urn:elevated': SUPER_ADMIN,
      'urn:not-selected': PAID,
    },
  }, ['urn:default', 'urn:elevated'])

  assert.deepEqual(properties, {
    keep: true,
    [APPLICATION_OPERATION_POLICY_PROPERTY]: {
      schema: 'kdcube.application_operations.v2',
      mode: 'selected',
      default_role: REGISTERED,
      operation_roles: { 'urn:elevated': SUPER_ADMIN },
    },
  })
  const parsed = applicationOperationRolePolicy(properties, { '*': [REGISTERED] })
  assert.deepEqual(parsed, {
    defaultRole: REGISTERED,
    operationRoles: { 'urn:elevated': SUPER_ADMIN },
  })
  assert.equal(applicationOperationRoleFor(parsed, 'urn:default'), REGISTERED)
  assert.equal(applicationOperationRoleFor(parsed, 'urn:elevated'), SUPER_ADMIN)
  assert.equal(applicationOperationRoleIsElevated(parsed, 'urn:elevated'), true)
})

test('application policy removal follows an explicit resource removal only', () => {
  const properties = {
    keep: true,
    [APPLICATION_OPERATION_POLICY_PROPERTY]: {
      schema: 'kdcube.application_operations.v2',
      mode: 'selected',
      default_role: REGISTERED,
      operation_roles: {},
    },
  }
  const policy = { defaultRole: PAID, operationRoles: {} }

  assert.deepEqual(applicationOperationPropertiesForSelection({
    properties,
    policy,
    selectedOperations: [],
    resourceSelected: false,
    resourcePreviouslySelected: false,
  }), properties)
  assert.deepEqual(applicationOperationPropertiesForSelection({
    properties,
    policy,
    selectedOperations: [],
    resourceSelected: false,
    resourcePreviouslySelected: true,
  }), { keep: true })
})

test('platform role options and operation checks use the shared dominance order', () => {
  assert.deepEqual(availableApplicationRoles([
    SUPER_ADMIN, REGISTERED, PRIVILEGED, REGISTERED, PAID,
  ]), [REGISTERED, PAID, PRIVILEGED, SUPER_ADMIN])
  assert.equal(projectedRoleAllows(SUPER_ADMIN, REGISTERED), true)
  assert.equal(projectedRoleAllows(PAID, PRIVILEGED), false)
  assert.equal(projectedRoleAllows(PRIVILEGED, 'kdcube:role:admin'), true)
})

test('one catalog ceiling admits only roles the grantor can delegate beneath it', () => {
  const grantorRoles = [SUPER_ADMIN, REGISTERED, PRIVILEGED, PAID, 'service:unrelated']

  assert.deepEqual(
    delegableApplicationRoles(grantorRoles, [SUPER_ADMIN]),
    [REGISTERED, PAID, PRIVILEGED, SUPER_ADMIN],
  )
  assert.deepEqual(
    delegableApplicationRoles(grantorRoles, [PAID]),
    [REGISTERED, PAID],
  )
  assert.equal(platformRoleAllowedBy(REGISTERED, [SUPER_ADMIN]), true)
  assert.equal(platformRoleAllowedBy(SUPER_ADMIN, [PRIVILEGED]), false)
  assert.deepEqual(delegableApplicationRoles([SUPER_ADMIN], [SUPER_ADMIN]), [SUPER_ADMIN])
  assert.deepEqual(unavailableApplicationPolicyRoles({
    defaultRole: REGISTERED,
    operationRoles: {
      'urn:selected': SUPER_ADMIN,
      'urn:stale': PRIVILEGED,
    },
  }, ['urn:selected'], [REGISTERED, PAID]), [SUPER_ADMIN])
  assert.deepEqual(unselectedApplicationOperationOverrides({
    defaultRole: REGISTERED,
    operationRoles: { 'urn:selected': PAID, 'urn:stale': SUPER_ADMIN },
  }, ['urn:selected']), ['urn:stale'])
})

test('stored unknown roles remain visible to the editor as unavailable', () => {
  const parsed = applicationOperationRolePolicy({
    [APPLICATION_OPERATION_POLICY_PROPERTY]: {
      schema: 'kdcube.application_operations.v2',
      mode: 'selected',
      default_role: 'kdcube:role:retired',
      operation_roles: { 'urn:a': 'kdcube:role:removed' },
    },
  }, { '*': [REGISTERED] })

  assert.deepEqual(parsed, {
    defaultRole: 'kdcube:role:retired',
    operationRoles: { 'urn:a': 'kdcube:role:removed' },
  })
  assert.deepEqual(unavailableApplicationPolicyRoles(
    parsed,
    ['urn:a'],
    [REGISTERED, PAID],
  ), ['kdcube:role:retired', 'kdcube:role:removed'])
})

test('AND and OR compose exact operation-to-role mappings like the runtime', () => {
  const caller = {
    defaultRole: REGISTERED,
    operationRoles: {
      'urn:a': SUPER_ADMIN,
      'urn:b': PAID,
    },
  }
  const control = {
    defaultRole: PAID,
    operationRoles: {
      'urn:a': PRIVILEGED,
      'urn:c': SUPER_ADMIN,
    },
  }

  assert.deepEqual(composeApplicationOperationRolePolicies(
    caller, control, ['urn:a', 'urn:b'], ['urn:a', 'urn:c'], 'and',
  ), {
    operations: ['urn:a'],
    policy: {
      defaultRole: REGISTERED,
      operationRoles: { 'urn:a': PRIVILEGED },
    },
  })
  assert.deepEqual(composeApplicationOperationRolePolicies(
    caller, control, ['urn:a', 'urn:b'], ['urn:a', 'urn:c'], 'or',
  ), {
    operations: ['urn:a', 'urn:b', 'urn:c'],
    policy: {
      defaultRole: PAID,
      operationRoles: {
        'urn:a': SUPER_ADMIN,
        'urn:c': SUPER_ADMIN,
      },
    },
  })
})
