import assert from 'node:assert/strict'
import test from 'node:test'

import { detailedCardOffersEdit } from '../src/features/delegatedAccess/cardActions.ts'

// Incident 2026-09-26: #220 opened linked Cards to read and drew Edit only for
// connected-app and manual Cards. A person's Control Card and My Card, opened
// from Team > People, showed Revoke alone. Every kind opened by a link must
// offer Edit (operator: "it must have all buttons on it").

const KINDS = {
  'agent Card': { access_id: 'agent-card-1', source: 'agent', client_id: 'agent:claude-code:one' },
  'project Control Card': { access_id: 'control-project-1', source: 'control' },
  'person Control Card': { access_id: 'control-person-1', source: 'control', issuer_kind: 'operator' },
  'My Card': { access_id: 'person-my-card-1', source: 'oauth', client_id: null },
  'connected client': { access_id: 'oauth-client-1', source: 'oauth', client_id: 'https://client.example/metadata' },
  'manual token': { access_id: 'manual-1', source: 'manual' },
}

for (const [kind, card] of Object.entries(KINDS)) {
  test(`${kind} opened by a link offers Edit`, () => {
    assert.equal(detailedCardOffersEdit(card, card.access_id), true)
  })
}

test('in the list, Edit stays where it was: agent, connected client and manual Cards', () => {
  assert.equal(detailedCardOffersEdit(KINDS['agent Card'], null), true)
  assert.equal(detailedCardOffersEdit(KINDS['connected client'], null), true)
  assert.equal(detailedCardOffersEdit(KINDS['manual token'], null), true)
  assert.equal(detailedCardOffersEdit(KINDS['project Control Card'], null), false)
  assert.equal(detailedCardOffersEdit(KINDS['My Card'], 'some-other-card'), false)
})
