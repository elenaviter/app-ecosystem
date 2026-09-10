import assert from 'node:assert/strict'
import test from 'node:test'

import { foldEntries } from '../src/components/foldRule.ts'

const tokens = (n) => Array.from({ length: n }, (_, i) => `t${i}`)

test('short rows are never folded', () => {
  assert.deepEqual(foldEntries(tokens(3), 3), { shown: tokens(3), hidden: 0, foldable: false })
  // 5 entries: hiding 2 behind "+2 more" saves nothing, so all stay visible.
  assert.deepEqual(foldEntries(tokens(5), 3), { shown: tokens(5), hidden: 0, foldable: false })
})

test('long rows show the limit and count the rest', () => {
  const fold = foldEntries(tokens(38), 3)
  assert.equal(fold.foldable, true)
  assert.deepEqual(fold.shown, ['t0', 't1', 't2'])
  assert.equal(fold.hidden, 35)
})

test('an open fold shows everything and stays foldable', () => {
  const fold = foldEntries(tokens(6), 3, true)
  assert.deepEqual(fold.shown, tokens(6))
  assert.equal(fold.hidden, 0)
  assert.equal(fold.foldable, true)
})
