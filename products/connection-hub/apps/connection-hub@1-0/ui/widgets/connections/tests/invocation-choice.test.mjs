import assert from 'node:assert/strict'
import test from 'node:test'

import { agentGrantWirePayload } from '../src/features/delegatedAccess/agentGrantPayload.ts'
import {
  editChangeId,
  focusedGrantArgs,
  pendingChoiceRequested,
  pendingFocusedIdentity,
  pendingPresetMode,
  splitEditedOperations,
} from '../src/features/delegatedAccess/invocationChoice.ts'

// Regression fixture: card oauth-6017dba3246fb090 (revision 2) grants `search`
// on the named-services door with policy search: always. A cached Claude call
// to `delete` was refused (operation_not_consented) and the recovery link asks
// the user to choose the policy for `delete`.
const RESOURCE = '*/api/integrations/bundles/*/*/kdcube-services@1-0/public/mcp/named_services*'
const card = {
  access_id: 'oauth-6017dba3246fb090',
  card_revision: 2,
  client_id: 'dcr-7c1e2b9a',
  source: 'oauth',
  resource_grants: { [RESOURCE]: ['named_services:use'] },
  resource_operations: { [RESOURCE]: ['search'] },
  invocation_policies: [{
    policy_id: 'pol-search',
    authority: { access_id: 'oauth-6017dba3246fb090', resource: RESOURCE, surface: 'outer', operation: 'search' },
    mode: 'always',
    revision: 1,
    state: 'available',
    remaining: null,
  }],
}
const pending = {
  clientId: 'dcr-7c1e2b9a',
  accessId: 'oauth-6017dba3246fb090',
  resource: RESOURCE,
  claims: ['named_services:use'],
  outerOperation: 'delete',
  invocationPolicy: 'choose',
  invocationChangeId: 'inv-9f2c1d',
}

test('the recovery request asks for a choice and names the exact focused identity', () => {
  assert.equal(pendingChoiceRequested(pending), true)
  assert.equal(pendingPresetMode(pending), null)
  assert.deepEqual(pendingFocusedIdentity(pending), {
    clientId: 'dcr-7c1e2b9a',
    accessId: 'oauth-6017dba3246fb090',
    resource: RESOURCE,
    operation: 'delete',
    changeId: 'inv-9f2c1d',
    requestBound: undefined,
    requestDigest: undefined,
    requestApprovalTicket: undefined,
    requestCardRevision: undefined,
    requestAuthorityRevision: undefined,
    approvalContext: undefined,
  })
  // A link missing the change id cannot commit a policy: no identity, no submit.
  assert.equal(pendingFocusedIdentity({ ...pending, invocationChangeId: '' }), null)
  assert.equal(pendingPresetMode({ ...pending, invocationPolicy: 'once' }), 'once')
})

for (const mode of ['once', 'always']) {
  test(`choosing ${mode} for delete submits exactly that operation with its mode and leaves search alone`, () => {
    const identity = pendingFocusedIdentity(pending)
    const payload = agentGrantWirePayload(focusedGrantArgs(identity, mode, card.resource_grants[RESOURCE]))
    assert.deepEqual(payload, {
      client_id: 'dcr-7c1e2b9a',
      resource: RESOURCE,
      claims: ['named_services:use'],
      label: '',
      access_id: 'oauth-6017dba3246fb090',
      invocation_mode: mode,
      invocation_change_id: 'inv-9f2c1d',
      resource_operations: { [RESOURCE]: ['delete'] },
    })
    // The server merges this ONE operation into the card and commits its
    // policy in the same transaction; `search` and its always policy are not
    // part of the payload, so nothing about them can move.
    assert.deepEqual(payload.resource_operations[RESOURCE], ['delete'])
    assert.ok(!('mode' in payload), 'no invocation_policy_set shape leaks into the grant payload')
    assert.ok(!('expected_revision' in payload))
  })
}

test('a request-bound choice carries its permit identity, nothing else changes', () => {
  const identity = pendingFocusedIdentity({
    ...pending,
    requestBound: true,
    requestDigest: 'abc123',
    requestApprovalTicket: 'ticket-1',
    requestCardRevision: 2,
    requestAuthorityRevision: 'cat-7',
    approvalContext: { application_id: 'app-1' },
  })
  const payload = agentGrantWirePayload(focusedGrantArgs(identity, 'once', ['named_services:use']))
  assert.equal(payload.request_bound, true)
  assert.equal(payload.request_digest, 'abc123')
  assert.equal(payload.request_approval_ticket, 'ticket-1')
  assert.equal(payload.request_card_revision, 2)
  assert.equal(payload.request_authority_revision, 'cat-7')
  assert.deepEqual(payload.approval_context, { application_id: 'app-1' })
  assert.deepEqual(payload.resource_operations[RESOURCE], ['delete'])
})

test('ordinary editing keeps granted operations on the card update and sends each new one through the focused grant', () => {
  const modes = { delete: 'once' }
  const split = splitEditedOperations(card.resource_operations[RESOURCE], ['search', 'delete'], (op) => modes[op])
  assert.deepEqual(split, { kept: ['search'], focused: [{ operation: 'delete', mode: 'once' }], missingChoice: [] })
  // Without a choice the save must wait: an operation granted through the
  // card update would be authorized always until a policy call followed.
  const undecided = splitEditedOperations(['search'], ['search', 'delete'], () => undefined)
  assert.deepEqual(undecided.missingChoice, ['delete'])
  assert.deepEqual(undecided.kept, ['search'])
  // Unticking a granted operation removes it from the update; no focused grant.
  assert.deepEqual(splitEditedOperations(['search'], [], () => 'always'), { kept: [], focused: [], missingChoice: [] })
})

test('an edit-minted change id is printable ASCII without spaces and bounded', () => {
  const id = editChangeId('oauth-6017dba3246fb090', 'delete', 'n0nce')
  assert.equal(id, 'edit:oauth-6017dba3246fb090:delete:n0nce')
  const odd = editChangeId('acc id', 'del eteé', 'x'.repeat(300))
  assert.ok(odd.length <= 256)
  assert.ok(/^[\x21-\x7e]+$/.test(odd))
})
