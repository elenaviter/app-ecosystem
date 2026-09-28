import assert from 'node:assert/strict'
import test from 'node:test'

import { RETURN_POLL_MS, RETURN_POLL_TRIES, accountsSignature } from '../src/features/delegatedToKdcube/oauthReturn.ts'

test('an approval that lands changes the accounts signature; a re-read of the same accounts does not', () => {
  const before = accountsSignature([{ account_id: 'a', status: 'connected', credential_status: 'active' }])
  assert.equal(before, accountsSignature([{ account_id: 'a', status: 'connected', credential_status: 'active' }]))
  assert.notEqual(before, accountsSignature([
    { account_id: 'a', status: 'connected', credential_status: 'active' },
    { account_id: 'b', status: 'connected', credential_status: 'active' },
  ]), 'a new account')
  assert.notEqual(
    accountsSignature([{ account_id: 'a', status: 'connected', credential_status: 'reconnect_required' }]),
    before,
    'a repaired credential',
  )
  assert.equal(accountsSignature(null), '')
  // Order never matters.
  assert.equal(
    accountsSignature([{ account_id: 'b' }, { account_id: 'a' }]),
    accountsSignature([{ account_id: 'a' }, { account_id: 'b' }]),
  )
})

test('the return watch is bounded: three minutes at most', () => {
  assert.equal(RETURN_POLL_MS * RETURN_POLL_TRIES, 180000)
})
