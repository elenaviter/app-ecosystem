import assert from 'node:assert/strict'
import test from 'node:test'

import {
  agentCapabilityCardLifecycle,
  isAgentCapabilityCard,
} from '../src/features/delegatedAccess/agentCardLifecycle.ts'
import { compareRecords, recordState } from '../src/features/delegatedAccess/grantFilter.ts'
import {
  AGENT_CAPABILITY_POLICY_SCHEMA,
  AGENT_CAPABILITY_SELECTION_PROPERTY,
} from '../src/features/delegatedAccess/agentCapabilitySelection.ts'

const now = 2_000_000_000
const resource = 'urn:kdcube:app:tenant:project:problem-board@1-0:worker'

function capabilityCard(overrides = {}) {
  return {
    access_id: 'stable-card-id',
    source: 'agent',
    card_revision: 7,
    expires_at: now + 60,
    control_card: {
      state: 'active',
      binding: {
        control_id: 'descriptor-control',
        issuer_ref: resource,
        issuer_kind: 'kdcube_agent_descriptor',
      },
    },
    properties: {
      [AGENT_CAPABILITY_SELECTION_PROPERTY]: {
        schema: AGENT_CAPABILITY_POLICY_SCHEMA,
        resource,
        capabilities: { tools: ['work.receive'] },
      },
    },
    ...overrides,
  }
}

test('a worker Agent Card never inherits descriptor Control defaults', () => {
  const worker = capabilityCard({
    control_card: {
      state: 'active',
      binding: {
        control_id: 'worker-control',
        issuer_ref: 'urn:kdcube:worker:session-1',
        issuer_kind: 'project_board_worker_policy',
      },
    },
  })

  assert.equal(isAgentCapabilityCard(worker), false)
  assert.equal(agentCapabilityCardLifecycle(worker, now, 'soon'), null)
})

test('an active capability Card auto-renews instead of appearing to expire soon', () => {
  const card = capabilityCard()
  const lifecycle = agentCapabilityCardLifecycle(card, now, '2033-05-18 03:34')

  assert.equal(isAgentCapabilityCard(card), true)
  assert.equal(recordState(card, now), 'active')
  assert.equal(lifecycle?.badge, 'auto-renews')
  assert.equal(
    lifecycle?.summary,
    'Card revision 7 · capability lease through 2033-05-18 03:34',
  )
  assert.match(lifecycle?.hint || '', /stable Card ID/)
  assert.match(lifecycle?.hint || '', /selected capability base/)

  const expiringCredential = {
    access_id: 'oauth-card',
    source: 'oauth',
    expires_at: now + 60,
  }
  assert.equal(recordState(expiringCredential, now), 'expiring')
  assert.ok(compareRecords('expiring', expiringCredential, card) < 0)
})

test('a lapsed capability Card grants nothing until its next message sync', () => {
  const card = capabilityCard({ expires_at: now - 1 })
  const lifecycle = agentCapabilityCardLifecycle(card, now, '2033-05-18 03:32')

  assert.equal(recordState(card, now), 'expired')
  assert.equal(lifecycle?.badge, 'awaiting sync')
  assert.match(lifecycle?.summary || '', /capability lease ended/)
  assert.match(lifecycle?.hint || '', /currently grants nothing/)
  assert.match(lifecycle?.hint || '', /next agent message renews it before tools are projected/)
  assert.match(lifecycle?.hint || '', /stable Card ID are preserved/)
})

test('credential-backed agent Cards retain ordinary credential expiry semantics', () => {
  const card = {
    access_id: 'consented-agent-card',
    source: 'agent',
    expires_at: now + 60,
  }

  assert.equal(isAgentCapabilityCard(card), false)
  assert.equal(agentCapabilityCardLifecycle(card, now, 'soon'), null)
  assert.equal(recordState(card, now), 'expiring')
})
