import assert from 'node:assert/strict'
import test from 'node:test'

import {
  commitEmailRequest,
  githubLinkRequest,
  githubStatusLine,
  githubStatusRequest,
  githubUnlinkRequest,
  isMyCard,
  linkableAccounts,
  myCardProjectRef,
  ownerAction,
  repositoriesFromSearch,
} from '../src/features/delegatedAccess/myCardGithub.ts'

const MY_CARD = {
  access_id: 'person-my-card-0123',
  source: 'project-person',
  issuer_kind: 'project-person',
  issuer_ref: 'work:project:quickstart',
}

test('a My Card is the project-person Card and names its project', () => {
  assert.equal(isMyCard(MY_CARD), true)
  assert.equal(myCardProjectRef(MY_CARD), 'work:project:quickstart')
  assert.equal(isMyCard({ ...MY_CARD, source: 'manual' }), false)
  assert.equal(isMyCard({ ...MY_CARD, issuer_ref: '' }), false)
  assert.equal(isMyCard(null), false)
  assert.equal(myCardProjectRef({ source: 'agent', issuer_ref: 'work:project:quickstart' }), '')
})

test('the four operations carry the project and only what each needs', () => {
  assert.deepEqual(githubStatusRequest('work:project:q'), {
    operation: 'project_person_github_key_status',
    data: { project_ref: 'work:project:q' },
  })
  assert.deepEqual(githubStatusRequest('work:project:q', ['o/r']).data, { project_ref: 'work:project:q', repositories: ['o/r'] })
  assert.deepEqual(githubLinkRequest('work:project:q', 'acct-1'), {
    operation: 'project_person_github_key_link',
    data: { project_ref: 'work:project:q', account_id: 'acct-1' },
  })
  assert.deepEqual(githubLinkRequest('work:project:q').data, { project_ref: 'work:project:q' })
  assert.equal(githubUnlinkRequest('work:project:q').operation, 'project_person_github_key_unlink')
  assert.deepEqual(commitEmailRequest('work:project:q', '  p@example.test '), {
    operation: 'project_person_commit_email_set',
    data: { project_ref: 'work:project:q', email: 'p@example.test' },
  })
})

test('repositories come from the opening link, owner/name only', () => {
  assert.deepEqual(
    repositoriesFromSearch('?access_id=x&repositories=example-org/app-ecosystem.git,,bad repo,example-org/applications,example-org/app-ecosystem'),
    ['example-org/app-ecosystem', 'example-org/applications'],
  )
  assert.deepEqual(repositoriesFromSearch('?access_id=x'), [])
})

test('the status line names the state and the login', () => {
  assert.deepEqual(githubStatusLine({ connection_state: 'connected', login: 'person' }), { tone: 'ok', text: 'Connected as person' })
  assert.equal(githubStatusLine({ connection_state: 'needs_reconnect', login: 'person' }).tone, 'warn')
  assert.deepEqual(githubStatusLine(null), { tone: 'off', text: 'Not connected for this project' })
})

test('an owner gap offers the link that fixes it, and a covered owner none', () => {
  assert.deepEqual(
    ownerAction({ owner: 'other', installed: false, install_url: 'https://github.com/apps/x/installations/new', repositories: [] }),
    { label: 'Install the app on other', url: 'https://github.com/apps/x/installations/new' },
  )
  assert.equal(
    ownerAction({
      owner: 'example-org', installed: true, install_url: 'https://github.com/settings/installations/1',
      repositories: [{ repository: 'example-org/a', state: 'missing' }],
    }).label,
    'Add the repositories on example-org',
  )
  assert.equal(
    ownerAction({ owner: 'example-org', installed: true, install_url: 'u', repositories: [{ repository: 'example-org/a', state: 'covered' }] }),
    null,
  )
})

test('only accounts of the GitHub provider the status names can be linked', () => {
  const accounts = [
    { account_id: 'g1', provider_id: 'github' },
    { account_id: 's1', provider_id: 'slack' },
  ]
  const status = { connection_state: 'not_connected', connect: { provider_id: 'github', connector_app_id: 'app', claims: [] } }
  assert.deepEqual(linkableAccounts(accounts, status).map((a) => a.account_id), ['g1'])
  assert.deepEqual(linkableAccounts(accounts, null), [])
})
