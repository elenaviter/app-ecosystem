import assert from 'node:assert/strict'
import test from 'node:test'

import {
  GITHUB_KEY_CHANGED,
  autoLinkAccount,
  commitEmailRequest,
  githubKeyChanged,
  githubLinkRequest,
  githubStatusLine,
  githubStatusRequest,
  githubUnlinkRequest,
  isMyCard,
  linkPendingKey,
  linkableAccounts,
  myCardProjectRef,
  openedRepositories,
  opensGithubSection,
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

test('embedded, the board passes repositories and the section in openParams, which win over the URL', () => {
  const openParams = { access_id: 'person-my-card-0123', section: 'github', repositories: 'example-org/app-ecosystem,example-org/applications' }
  assert.deepEqual(openedRepositories(openParams, '?repositories=example-org/other'), ['example-org/app-ecosystem', 'example-org/applications'])
  assert.deepEqual(openedRepositories(undefined, '?repositories=example-org/other'), ['example-org/other'])
  assert.deepEqual(openedRepositories({ access_id: 'x' }, '?repositories=example-org/other'), ['example-org/other'])
  assert.equal(opensGithubSection(openParams, ''), true)
  assert.equal(opensGithubSection(undefined, '?section=github'), true)
  assert.equal(opensGithubSection({}, ''), false)
})

test('a Connect made here links on return only when it is pending and exactly one GitHub account is there', () => {
  const status = { connection_state: 'not_connected', connect: { provider_id: 'github', connector_app_id: 'app', claims: [] } }
  const one = [{ account_id: 'g1', provider_id: 'github' }, { account_id: 's1', provider_id: 'slack' }]
  const two = [...one, { account_id: 'g2', provider_id: 'github' }]
  assert.equal(autoLinkAccount(status, one, true), 'g1')
  assert.equal(autoLinkAccount(status, one, false), '', 'opening My Card never links on its own')
  assert.equal(autoLinkAccount(status, two, true), '', 'several accounts: the person chooses')
  assert.equal(autoLinkAccount({ ...status, connection_state: 'connected' }, one, true), '')
  assert.equal(autoLinkAccount(null, one, true), '')
  assert.equal(linkPendingKey('work:project:q'), 'kdc-github-link-pending:work:project:q')
})

test('the change is said in the board\'s terms', () => {
  assert.deepEqual(githubKeyChanged('work:project:q', 'connected'), {
    type: GITHUB_KEY_CHANGED, project_ref: 'work:project:q', connection_state: 'connected',
  })
  assert.equal(GITHUB_KEY_CHANGED, 'connection_hub.github_key.changed')
})
