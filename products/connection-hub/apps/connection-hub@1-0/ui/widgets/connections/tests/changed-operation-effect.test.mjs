import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { changedOperationsSuspended } from '../src/features/delegatedAccess/resourceEditing.ts'

// Operator, 2026-09-27: the project Control Card listed Problem Board
// operations as "Changed, suspended until you accept" while agents ran them.
// A catalog row's call never checks the accepted descriptor, so its change is
// in effect and waits for review; only a remote MCP connector suspends.

test('the server decides: suspended only where the call checks the accepted descriptor', () => {
  assert.equal(changedOperationsSuspended({ status: 'changed', kind: 'catalog', changed_effect: 'in_effect_review' }), false)
  assert.equal(changedOperationsSuspended({ status: 'changed', kind: 'remote_mcp', changed_effect: 'suspended_until_accepted' }), true)
  // The server's word wins over the kind.
  assert.equal(changedOperationsSuspended({ status: 'changed', kind: 'catalog', changed_effect: 'suspended_until_accepted' }), true)
})

test('without the server\'s word, only a remote MCP connector suspends', () => {
  assert.equal(changedOperationsSuspended({ status: 'changed', kind: 'remote_mcp' }), true)
  assert.equal(changedOperationsSuspended({ status: 'changed', kind: 'catalog' }), false)
  assert.equal(changedOperationsSuspended({ status: 'changed' }), false)
  assert.equal(changedOperationsSuspended(undefined), false)
})

test('the review words each case as what happens', () => {
  const parts = readFileSync(new URL('../src/features/delegatedAccess/ResourceEditorParts.tsx', import.meta.url), 'utf8')
  const review = parts.slice(parts.indexOf('export function ResourceDriftReview('))
  assert.match(review, /<span className="badge badge-warn">Suspended<\/span>\s*<small>Not run until its updated descriptor is accepted\.<\/small>/)
  assert.match(review, /<span className="badge badge-neutral">In effect<\/span>\s*<small>Still runs\. The service changed how it describes this tool; accepting records your review\.<\/small>/)
  // Accepting is offered in both cases: it records the review.
  assert.match(review, /\{on \? 'Update accepted' : 'Accept update'\}/)
})
