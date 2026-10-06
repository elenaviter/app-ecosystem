import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  applyDelegatedAccessRevokeResult,
  delegatedAccessRevokePayload,
  isRevokeRevisionConflict,
} from '../src/features/delegatedAccess/delegatedAccessRevoke.ts'

const target = { access_id: 'issuer-card-1', card_revision: 7 }

test('plain revoke and copied JSON preserve the displayed target pair', () => {
  const payload = delegatedAccessRevokePayload(target.access_id, target.card_revision)
  assert.deepEqual(payload, {
    access_id: 'issuer-card-1', expected_access_id: 'issuer-card-1', expected_card_revision: 7,
  })
  // The copied command serializes the very same helper, not an ID-only body.
  assert.deepEqual(JSON.parse(JSON.stringify(payload)), payload)
  target.card_revision = 8
  assert.equal(payload.expected_card_revision, 7)
  target.card_revision = 7
})

test('legacy records retain the owner-only request; no revision is invented', () => {
  assert.deepEqual(delegatedAccessRevokePayload('legacy-owner-card'), {
    access_id: 'legacy-owner-card',
  })
})

function state() {
  return {
    busy: true, error: '', items: [target, { access_id: 'other-card' }],
    focusedCard: target, issuedAccess: target, issuedToken: 'synthetic-token', issuedHeader: 'synthetic-header',
  }
}

test('409 revision conflict keeps the Card and banner and asks for a new confirmation', () => {
  const current = state()
  const before = structuredClone(current)
  const conflict = { ok: false, status: 409, error: 'delegated_card_revision_conflict' }
  assert.equal(isRevokeRevisionConflict(conflict), true)
  applyDelegatedAccessRevokeResult(current, conflict, target.access_id, 7)
  assert.equal(current.busy, false)
  assert.deepEqual(current.items, before.items)
  assert.deepEqual(current.focusedCard, before.focusedCard)
  assert.deepEqual(current.issuedAccess, before.issuedAccess)
  assert.equal(current.issuedToken, before.issuedToken)
  assert.equal(current.issuedHeader, before.issuedHeader)
  assert.match(current.error, /revision 7/)
  assert.match(current.error, /confirm again/)
  assert.match(current.error, /no revocation was retried/)
})

test('an unrelated refusal is not mistaken for a removal or revision conflict', () => {
  const current = state()
  const refusal = { ok: false, status: 409, error: 'issuer_write_refused', message: 'Issuer refused.' }
  assert.equal(isRevokeRevisionConflict(refusal), false)
  applyDelegatedAccessRevokeResult(current, refusal, target.access_id, 7)
  assert.equal(current.items.length, 2)
  assert.equal(current.focusedCard.access_id, target.access_id)
  assert.equal(current.error, 'Issuer refused.')
})

test('successful revocation still removes only its exact Card and clears its banner', () => {
  const current = state()
  applyDelegatedAccessRevokeResult(current, { ok: true, removed: true }, target.access_id, 7)
  assert.deepEqual(current.items, [{ access_id: 'other-card' }])
  assert.equal(current.focusedCard, undefined)
  assert.equal(current.issuedAccess, undefined)
  assert.equal(current.issuedToken, '')
  assert.equal(current.issuedHeader, '')
})

test('widget integration sends the pair, keeps 409 results and refreshes reads without retry', () => {
  const panel = readFileSync(new URL('../src/features/delegatedAccess/DelegatedAccessPanel.tsx', import.meta.url), 'utf8')
  const slice = readFileSync(new URL('../src/features/delegatedAccess/delegatedAccessSlice.ts', import.meta.url), 'utf8')
  const revoke = panel.slice(panel.indexOf('const revoke = async'), panel.indexOf('// Expiry as the server saw it'))
  assert.match(revoke, /setConfirmRevokeId\(null\)/)
  assert.match(revoke, /expectedCardRevision: item\.card_revision/)
  assert.equal((revoke.match(/dispatch\(revokeDelegatedAccess\(/g) || []).length, 1)
  assert.match(revoke, /dispatch\(loadDelegatedAccess\(\)\)/)
  assert.match(revoke, /isRevokeRevisionConflict\(result\)/)
  assert.match(revoke, /dispatch\(loadControlCard\(/)
  assert.match(panel, /JSON\.stringify\(delegatedAccessRevokePayload\(accessId, item\.card_revision\)\)/)
  assert.match(panel, /Revoke\{item\.card_revision !== undefined/)
  const thunk = slice.slice(slice.indexOf('export const revokeDelegatedAccess'), slice.indexOf('/** A fresh token'))
  assert.match(thunk, /delegatedAccessRevokePayload\(accessId, expectedCardRevision\)/)
  assert.match(thunk, /if \(res\?\.ok === false && res\?\.status === 409\) return res/)
  assert.match(slice, /applyDelegatedAccessRevokeResult\(/)
})
