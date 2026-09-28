// 2026-09-28 ~21:20Z: the operator authorized a new coordinator agent on the
// consent page, added a resource and an application operation as on the
// project's Control Card, left the defaults, and Approve failed with the raw
// JSON {"error":"oauth_consent_invocation_policy_invalid", ...}. The platform
// requires one Once/Always for every selected outer operation; the page gave
// an added operation no mode and sent the application operations (resource
// "*") as {}.
import assert from 'node:assert/strict'
import test from 'node:test'

import {
  DEFAULT_INVOCATION_MODE,
  submittedInvocationModes,
  withDefaultInvocationModes,
} from '../src/features/delegatedAccess/invocationChoice.ts'

const BOARD = '*/api/integrations/bundles/*/*/problem-board@1-0/public/mcp/problem_board*'
const SERVICES = 'https://runtime.example.test/api/integrations/bundles/t/p/kdcube-services@1-0/public/mcp*'

/** The platform's own rule (consent decision route): the submitted policy keys
 *  equal the selected (resource, operation) keys, and each is once or always. */
function platformAccepts(resourceOperations, policies) {
  const selected = new Set(Object.entries(resourceOperations).flatMap(([resource, ops]) => ops.map((op) => `${resource}|${op}`)))
  const submitted = Object.entries(policies).flatMap(([resource, ops]) => Object.entries(ops).map(([op, mode]) => [`${resource}|${op}`, mode]))
  return submitted.length === selected.size
    && submitted.every(([key, mode]) => selected.has(key) && (mode === 'once' || mode === 'always'))
}

test('an added operation starts as Every time, like the draft proposes, and a choice is kept', () => {
  assert.equal(DEFAULT_INVOCATION_MODE, 'always')
  const modes = withDefaultInvocationModes({ [`${BOARD}:review.accept`]: 'once' }, BOARD, ['review.accept', 'review.return'])
  assert.deepEqual(modes, { [`${BOARD}:review.accept`]: 'once', [`${BOARD}:review.return`]: 'always' })
})

test('a resource added on the consent page and approved with defaults satisfies the platform', () => {
  // The draft's Problem Board rows, as the platform seeds them.
  let modes = { [`${BOARD}:worker.heartbeat`]: 'always' }
  const resourceOperations = {
    [BOARD]: ['worker.heartbeat'],
    [SERVICES]: ['services.search', 'services.call'],
    '*': ['kdcube.conversations.list'],
  }
  // The person adds the services resource ("All") and one application operation.
  modes = withDefaultInvocationModes(modes, SERVICES, resourceOperations[SERVICES])
  modes = withDefaultInvocationModes(modes, '*', resourceOperations['*'])

  const policies = submittedInvocationModes([BOARD, SERVICES, '*'], resourceOperations, modes)

  assert.ok(platformAccepts(resourceOperations, policies), JSON.stringify(policies))
  assert.deepEqual(policies['*'], { 'kdcube.conversations.list': 'always' }, 'application operations are not sent as {}')
})

test('an operation that somehow has no mode is still sent with the default, never without one', () => {
  const policies = submittedInvocationModes([SERVICES], { [SERVICES]: ['services.call'] }, {})
  assert.deepEqual(policies, { [SERVICES]: { 'services.call': 'always' } })
})

test('a refused call shows the server sentence, not its JSON body', async () => {
  const { errorDetail } = await import('../src/api/errorDetail.ts')
  const body = { error: 'oauth_consent_invocation_policy_invalid', error_description: 'every selected outer operation requires Once or Always' }
  assert.equal(errorDetail(body, JSON.stringify(body)), 'every selected outer operation requires Once or Always')
  assert.equal(errorDetail({ detail: 'Not allowed' }, 'x'), 'Not allowed')
  assert.equal(errorDetail({ error: 'card_conflict' }, 'x'), 'card_conflict')
  assert.equal(errorDetail({ raw: '<html>' }, 'Bad Gateway'), 'Bad Gateway')
})
