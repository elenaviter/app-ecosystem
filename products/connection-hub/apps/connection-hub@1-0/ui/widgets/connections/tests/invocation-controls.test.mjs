import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const controls = readFileSync(
  new URL('../src/features/delegatedAccess/InvocationControls.tsx', import.meta.url),
  'utf8',
)

test('invocation policy controls keep Every time before Once in every state', () => {
  const liveStart = controls.indexOf('export function InvocationPolicyControl')
  const choiceStart = controls.indexOf('export function OperationInvocationChoice')
  const live = controls.slice(liveStart, choiceStart)
  const choice = controls.slice(choiceStart)

  for (const component of [live, choice]) {
    assert.ok(component.indexOf('Every time') >= 0)
    assert.ok(component.indexOf('Once') >= 0)
    assert.ok(component.indexOf('Every time') < component.indexOf('Once'))
  }
})
