import assert from 'node:assert/strict'
import test from 'node:test'

import {
  commonOperationGrants,
  doorGrantsForOperation,
  pendingServiceApprovalReady,
  pendingSelectionStatus,
  proposeExactAccountClaim,
  resolvePendingServiceCapability,
} from '../src/features/delegatedAccess/pendingGrantProjection.ts'

const resource = {
  resource: '*/public/mcp/named_services*',
  operations: [
    { name: 'schema', grants: ['named_services:use'] },
    { name: 'call', grants: ['named_services:use', 'named_services:invoke'] },
  ],
  named_services: [{
    namespace: 'slack',
    connected_accounts: [{
      provider_id: 'slack',
      claims_by_operation: { 'object.action.post_message': ['slack:post'] },
    }],
  }],
}

test('pending service capability derives its door grants from the active catalog', () => {
  const capability = resolvePendingServiceCapability(
    {
      resource: resource.resource,
      namespace: 'slack',
      operation: 'object.action.post_message',
    },
    [resource],
    () => [{
      operation: 'object.action.post_message',
      label: 'Post Slack message',
      description: 'Post a message to a Slack conversation.',
      grants: ['slack:post'],
    }],
  )

  assert.equal(capability?.operation.label, 'Post Slack message')
  assert.deepEqual(capability?.requiredDoorGrants, ['named_services:use'])
  assert.deepEqual(capability?.accountRequirements, [{
    providerId: 'slack',
    claims: ['slack:post'],
  }])
  assert.equal(capability?.namespace.connected_accounts?.[0].provider_id, 'slack')
})

test('common grants require membership on every outer operation', () => {
  assert.deepEqual(commonOperationGrants(resource), ['named_services:use'])
})

test('provider-backed operation claims are not copied into door grants', () => {
  const namespace = resource.named_services[0]
  assert.deepEqual(doorGrantsForOperation(
    resource,
    namespace,
    'object.action.post_message',
    ['named_services:use', 'slack:post'],
  ), ['named_services:use'])
})

test('focused consent labels persisted, proposed, and required selections', () => {
  assert.equal(pendingSelectionStatus(true, true, true), 'Already granted')
  assert.equal(pendingSelectionStatus(false, true, true), 'Pending - not granted yet')
  assert.equal(
    pendingSelectionStatus(false, true, false),
    'Pending - not granted yet',
    'a newly selected optional account claim is still a pending proposal',
  )
  assert.equal(pendingSelectionStatus(false, false, true), 'Required for this request')
  assert.equal(pendingSelectionStatus(false, false, false), null)
})

test('account claims cannot make an unlisted operation grantable', () => {
  assert.equal(resolvePendingServiceCapability(
    { resource: resource.resource, namespace: 'slack', operation: 'missing' },
    [resource],
    () => [],
  ), null)
})

test('operation requirements without provider metadata remain door grants', () => {
  const capability = resolvePendingServiceCapability(
    { resource: resource.resource, namespace: 'slack', operation: 'object.schema' },
    [{ ...resource, named_services: [{ namespace: 'slack' }] }],
    () => [{
      operation: 'object.schema',
      label: 'Slack schema',
      description: '',
      grants: ['named_services:use'],
    }],
  )

  assert.deepEqual(capability?.requiredDoorGrants, ['named_services:use'])
  assert.deepEqual(capability?.accountRequirements, [])
})

test('account claims never make an unselected operation ready', () => {
  const capability = resolvePendingServiceCapability(
    {
      resource: resource.resource,
      namespace: 'slack',
      operation: 'object.action.post_message',
    },
    [resource],
    () => [{
      operation: 'object.action.post_message',
      label: 'Post Slack message',
      description: '',
      grants: ['slack:post'],
    }],
  )

  assert.equal(pendingServiceApprovalReady(
    capability,
    false,
    ['named_services:use'],
    { slack: { account_1: ['slack:post'] } },
  ), false)
})

test('selected operation still requires its door and account selections', () => {
  const capability = resolvePendingServiceCapability(
    {
      resource: resource.resource,
      namespace: 'slack',
      operation: 'object.action.post_message',
    },
    [resource],
    () => [{
      operation: 'object.action.post_message',
      label: 'Post Slack message',
      description: '',
      grants: ['slack:post'],
    }],
  )

  assert.equal(pendingServiceApprovalReady(
    capability,
    true,
    [],
    { slack: { account_1: ['slack:post'] } },
  ), false)
  assert.equal(pendingServiceApprovalReady(
    capability,
    true,
    ['named_services:use'],
    {},
  ), false)
  assert.equal(pendingServiceApprovalReady(
    capability,
    true,
    ['named_services:use'],
    { slack: { account_1: ['slack:post'] } },
  ), true)
})

test('an exact account demand proposes the claim on that account only', () => {
  const capability = resolvePendingServiceCapability(
    {
      resource: resource.resource,
      namespace: 'slack',
      operation: 'object.action.post_message',
    },
    [resource],
    () => [{
      operation: 'object.action.post_message',
      label: 'Post Slack message',
      description: '',
      grants: ['slack:post'],
    }],
  )
  const accounts = [
    { account_id: 'slack_a', provider_id: 'slack', claims: ['slack:post'] },
    { account_id: 'slack_b', provider_id: 'slack', claims: ['slack:post'] },
  ]

  assert.deepEqual(proposeExactAccountClaim(
    {},
    capability,
    accounts,
    'slack_b',
    'slack:post',
  ), { slack: { slack_b: ['slack:post'] } })
})

test('an account is never guessed when the denial names no account', () => {
  const capability = resolvePendingServiceCapability(
    {
      resource: resource.resource,
      namespace: 'slack',
      operation: 'object.action.post_message',
    },
    [resource],
    () => [{
      operation: 'object.action.post_message',
      label: 'Post Slack message',
      description: '',
      grants: ['slack:post'],
    }],
  )

  assert.deepEqual(proposeExactAccountClaim(
    {},
    capability,
    [{ account_id: 'slack_a', provider_id: 'slack', claims: ['slack:post'] }],
    undefined,
    'slack:post',
  ), {})
})
