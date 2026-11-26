import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  applicationHolder,
  controlCardGetRequest,
  linkedControlOpenTarget,
} from '../src/features/delegatedAccess/projectPersonControl.ts'

// One descriptor Control per agent, held by the application. The editor
// names that holder on every read and save; the Hub checks it against the
// Control's own issuer, so the value only routes, it never authorizes.
const HOLDER = 'application-authority:' + 'a'.repeat(64)
const agentCard = { source: 'agent', issuer_kind: '', issuer_ref: '', grantor_subject: 'user-1' }

test('the Agent Card opens its shared descriptor Control through the application holder', () => {
  const binding = {
    control_id: 'control-shared', issuer_ref: 'urn:kdcube:app:t:p:app@1-0:worker',
    issuer_kind: 'kdcube_agent_descriptor', holder_subject: HOLDER,
  }
  const target = linkedControlOpenTarget(agentCard, binding)
  assert.deepEqual(target, { controlId: 'control-shared', holderSubject: HOLDER })
  assert.deepEqual(controlCardGetRequest(target), {
    operation: 'control_card_get',
    data: { control_id: 'control-shared', holder_subject: HOLDER },
  })
})

test('a legacy per-user copy and any other Control keep the plain read', () => {
  for (const binding of [
    { control_id: 'control-own', issuer_ref: 'urn:kdcube:app:t:p:app@1-0:worker', issuer_kind: 'kdcube_agent_descriptor' },
    { control_id: 'control-own', issuer_ref: 'urn:kdcube:app:t:p:app@1-0:worker', issuer_kind: 'kdcube_agent_descriptor', holder_subject: 'user-1' },
    { control_id: 'control-app', issuer_ref: 'urn:x', issuer_kind: 'application', holder_subject: HOLDER },
  ]) {
    assert.deepEqual(linkedControlOpenTarget(agentCard, binding), { controlId: binding.control_id })
  }
})

test('a loaded shared Control is saved and returned for its holder', () => {
  assert.equal(applicationHolder({ issuer_kind: 'kdcube_agent_descriptor', grantor_subject: HOLDER }), HOLDER)
  assert.equal(applicationHolder({ issuer_kind: 'kdcube_agent_descriptor', grantor_subject: 'user-1' }), '')
  assert.equal(applicationHolder({ issuer_kind: 'application', grantor_subject: HOLDER }), '')

  const slice = readFileSync(new URL('../src/features/delegatedAccess/delegatedAccessSlice.ts', import.meta.url), 'utf8')
  const panel = readFileSync(new URL('../src/features/delegatedAccess/DelegatedAccessPanel.tsx', import.meta.url), 'utf8')
  assert.match(slice, /access_id: accessId, \.\.\.\(holderSubject \? \{ holder_subject: holderSubject \} : \{\}\)/)
  assert.match(slice, /return_to_descriptor: true/)
  assert.match(slice, /\.\.\.\(holderSubject \? \{ holder_subject: holderSubject \} : \{\}\),\n\s+return_to_descriptor: true/)
  assert.match(panel, /holderSubject: applicationHolder\(item\) \|\| undefined,/)
  assert.match(panel, /holderSubject: pending\.holderSubject,/)
})
