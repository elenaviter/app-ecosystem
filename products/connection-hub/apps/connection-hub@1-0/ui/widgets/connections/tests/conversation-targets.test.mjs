import assert from 'node:assert/strict'
import test from 'node:test'

import {
  CONVERSATION_TARGETS_PROPERTY,
  cardConversationTargets,
  withConversationTargets,
} from '../src/features/delegatedAccess/conversationTargets.ts'

test('Card conversation targets remain exact and do not overwrite other properties', () => {
  const properties = withConversationTargets(
    { 'kdcube.application_operations': { default_role: 'registered' } },
    ['workspace@1-0', 'problem-board@1-0', 'workspace@1-0'],
  )
  assert.deepEqual(properties[CONVERSATION_TARGETS_PROPERTY], ['problem-board@1-0', 'workspace@1-0'])
  assert.deepEqual(cardConversationTargets(properties), ['problem-board@1-0', 'workspace@1-0'])
  assert.deepEqual(properties['kdcube.application_operations'], { default_role: 'registered' })
})

test('malformed and wildcard target selections are not shown as grants', () => {
  assert.deepEqual(cardConversationTargets({ [CONVERSATION_TARGETS_PROPERTY]: '*' }), [])
  assert.deepEqual(cardConversationTargets({ [CONVERSATION_TARGETS_PROPERTY]: ['valid-app', '*'] }), [])
})

test('editing another Card does not introduce a target property', () => {
  assert.deepEqual(withConversationTargets({ label: 'Other access' }, []), { label: 'Other access' })
})
