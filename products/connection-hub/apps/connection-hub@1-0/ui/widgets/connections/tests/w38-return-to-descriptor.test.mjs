import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const read = (path) => readFileSync(new URL(`../src/${path}`, import.meta.url), 'utf8')

test('W38: Return to descriptor is one update request carrying only the return flag and the pins', () => {
  const slice = read('features/delegatedAccess/delegatedAccessSlice.ts')
  const thunk = slice.slice(slice.indexOf('export const returnControlToDescriptor'), slice.indexOf('export const PERSON_REMOVAL_GUIDANCE'))
  assert.match(thunk, /postOp<DelegatedAccessCreateResult>\('delegated_access_update'/)
  assert.match(thunk, /return_to_descriptor: true/)
  assert.match(thunk, /expected_card_revision: expectedCardRevision/)
  assert.doesNotMatch(thunk, /resource_grants|named_service_operations|selected_capabilities/)
})

test('W38: the button is offered only on a descriptor Control an administrator edited, behind a confirmation', () => {
  const panel = read('features/delegatedAccess/DelegatedAccessPanel.tsx')
  assert.match(panel, /record\.provenance\?\.agent_descriptor_card_edited === true/)
  assert.match(panel, /item\?\.kind === 'kdcube_agent_descriptor'/)
  assert.match(panel, />\s*Return to descriptor\s*<\/button>/)
  assert.match(panel, /onClick=\{\(\) => setConfirmReturnToDescriptor\(\{/)
  assert.match(panel, /\{renderReturnToDescriptorDialog\(\)\}/)
  assert.match(panel, /dispatch\(returnControlToDescriptor\(\{/)
})
