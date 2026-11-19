// kdcube#270 F9 (2026-09-29): after a 409 "Your draft is still here", a second Save stored the Slack
// operation but reset the model, instruction and skill. The Agent Card editor then saved in two writes:
// the Card selection, and the KDCube agent defaults beside it. The first write moved the revision, so the
// second one was refused, and only half of the draft landed. Since W755 a Save is one
// delegated_access_update. The agent defaults travel in the same request as the Card's properties, and a
// refusal stores nothing. These checks pin that, so a split save cannot come back.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const panel = readFileSync(
  new URL('../src/features/delegatedAccess/DelegatedAccessPanel.tsx', import.meta.url),
  'utf8',
)
const start = panel.indexOf('const saveEdit = async (item: DelegatedAccessRecord) => {')
const save = panel.slice(start, panel.indexOf('\n  };\n', start))

test('Save sends the agent defaults inside the one Card update', () => {
  assert.ok(start > 0, 'the editor Save is found')
  assert.match(save, /selectedCapabilities: editAgentCapabilities,/, 'the drafted model, instruction and skills are read')
  assert.match(save, /properties: editedProperties,/, 'they go into the update request as the Card properties')
  assert.equal(save.split('dispatch(updateDelegatedAccess(').length - 1, 1, 'exactly one Card write per Save')
  assert.doesNotMatch(save, /updateAgentCapabilitySelection\(/, 'no second write for the agent defaults')
})

test('the separate agent-defaults write exists only for Reset to Control defaults', () => {
  const calls = panel.split('dispatch(updateAgentCapabilitySelection(').length - 1
  assert.equal(calls, 1)
  const reset = panel.slice(panel.indexOf('const resetAgentToControlDefaults = async'))
  assert.ok(reset.indexOf('dispatch(updateAgentCapabilitySelection(') > 0)
  assert.ok(reset.indexOf('dispatch(updateAgentCapabilitySelection(') < reset.indexOf('const returnToDescriptor = async'))
})

test('a refused Save keeps the whole draft and writes nothing more', () => {
  const refused = save.slice(save.indexOf('if (!updated || updated.ok === false) {'))
  assert.ok(refused.length > 0)
  const firstReturn = refused.indexOf('return;')
  assert.ok(firstReturn > 0)
  assert.doesNotMatch(refused.slice(0, firstReturn), /clearEditState\(\)|dispatch\(/, 'a refusal neither clears the draft nor writes again')
})
