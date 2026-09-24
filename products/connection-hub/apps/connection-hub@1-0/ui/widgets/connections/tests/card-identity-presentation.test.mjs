import assert from 'node:assert/strict'
import test from 'node:test'

import {
  agentCardPresentation,
  runtimeHostLabel,
} from '../src/features/delegatedAccess/cardIdentityPresentation.ts'

test('worker Card titles lead with the agent and host', () => {
  assert.deepEqual(agentCardPresentation({
    client_metadata: {
      kdcube_worker_alias: 'claude-ops',
      kdcube_machine_label: 'spark1',
      kdcube_agent_provider: 'claude-code',
      kdcube_agent_session_id: 'a7b7935d-session',
    },
  }), {
    title: 'claude-ops@spark1',
    subtitle: 'Claude Code · session a7b7935d-session',
  })
})

test('machine id is the readable host fallback', () => {
  assert.equal(runtimeHostLabel({ kdcube_machine_id: 'machine-1' }), 'machine-1')
  assert.equal(agentCardPresentation({ client_metadata: {} }), null)
})
