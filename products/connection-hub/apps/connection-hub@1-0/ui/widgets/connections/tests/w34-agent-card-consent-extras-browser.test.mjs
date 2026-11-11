// W34: the Agent Card consent review offers more access within the agent's Control and only adds;
// a provider permission nobody holds is requested at its provider after saving the selection.
// Mounted: the real DelegatedAccessPanel and store in Chromium, the Hub the recording bridge of the W1018 fixture
// (tests/fixtures/w1018-agent-card-consent.tsx). The Card holds slides object.list; its Control also allows
// slides provider.about and object.upsert, sheets values.update, memories:read and the records_export tool.
import assert from 'node:assert/strict'
import test from 'node:test'

import { launchBrowser, startFixtureServer } from './browser-fixture.mjs'

const NAMED = '*/api/integrations/bundles/*/*/kdcube-services@1-0/public/mcp/named_services*'
const MEMORIES = '*/api/integrations/bundles/*/*/kdcube-services@1-0/public/mcp/memories*'
const SERVICE = 'https://service.example/mcp'
const CLIENT = 'kdcube-agent:workspace@2026-03-31-13-36:main'

async function open(browser, origin, variant) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 1600 } })
  await page.route('**/*', (route) => route.request().url().endsWith('/csrf')
    ? route.fulfill({ status: 200, contentType: 'application/json', body: '{"csrf_required":false}' })
    : route.request().url().startsWith(origin) ? route.continue() : route.abort())
  const errors = []
  page.on('pageerror', (error) => errors.push(String(error)))
  const url = `${origin}tests/fixtures/w1018-agent-card-consent.html?variant=${variant}`
  await page.goto(url, { waitUntil: 'load' })
  await page.waitForTimeout(2000)
  await page.goto(url, { waitUntil: 'domcontentloaded' })
  await page.getByTestId('agent-card-consent').waitFor({ timeout: 15000 })
  return { page, errors }
}

const grantsOf = (calls) => calls.filter((call) => call.operation === 'delegated_agent_grant_create').map((call) => call.data)
const grants = (page) => page.evaluate(() => window.__calls).then(grantsOf)
const extras = (page) => page.getByTestId('agent-card-consent-extras')
const operation = (page, key) => extras(page).getByTestId('agent-card-consent-extra-operation').filter({ hasText: key }).locator('input')

// Open the extras and every group inside them (each group is its own disclosure).
async function expand(page) {
  await page.evaluate(() => document.querySelectorAll('[data-testid=agent-card-consent-extras], [data-testid=agent-card-consent-extras] details')
    .forEach((node) => { node.open = true }))
}

test('W34: more access on the agent Card consent review', async (t) => {
  const browser = await launchBrowser()
  if (!browser) {
    t.skip('no Chromium available; set PLAYWRIGHT_CHROMIUM_EXECUTABLE to run the mounted W34 check')
    return
  }
  const server = await startFixtureServer()
  const origin = server.resolvedUrls.local[0]
  try {
    await t.test('the extras are offered closed, within the Control, held entries fixed and outside entries disabled', async () => {
      const { page, errors } = await open(browser, origin, 'account-only')
      assert.equal(await extras(page).evaluate((node) => node.open), false, 'nothing beyond the request is open or selected')
      assert.ok((await extras(page).locator('summary').first().innerText()).includes('Open to choose'), 'the closed section says it opens')
      assert.equal(await extras(page).locator('summary').first().evaluate((node) => getComputedStyle(node).cursor), 'pointer')
      await expand(page)
      assert.equal(await operation(page, 'object.list').isChecked(), true, 'the held operation is shown ticked')
      assert.equal(await operation(page, 'object.list').isDisabled(), true, 'and is never sent as a change')
      assert.equal(await operation(page, 'provider.capabilities').isDisabled(), true, 'outside the Control')
      assert.ok((await extras(page).getByTestId('agent-card-consent-extra-operation').filter({ hasText: 'provider.capabilities' }).innerText())
        .toLowerCase().includes("not in this agent's control"))
      assert.equal(await operation(page, 'object.upsert').isDisabled(), false)
      const choice = extras(page).getByTestId('agent-card-consent-extra-operation').filter({ hasText: 'object.upsert' })
      assert.equal(await choice.evaluate((node) => getComputedStyle(node).opacity), '1', 'an open choice is not faded')
      await choice.locator("input").check()
      assert.ok(await choice.evaluate((node) => node.classList.contains('agent-card-consent-picked')), 'a picked row is marked')
      await choice.locator("input").uncheck()
      assert.deepEqual(await extras(page).getByTestId('agent-card-consent-extra-grant-group').locator('summary code').allInnerTexts(),
        ['memories', 'named_services', 'slides'], 'permissions are grouped by service')
      assert.equal(await operation(page, 'values.update').isDisabled(), false, 'another namespace the Control allows')
      const memories = extras(page).getByTestId('agent-card-consent-extra-grant').filter({ hasText: 'memories:read' }).locator('input')
      assert.equal(await memories.isDisabled(), false, 'another service the Control allows')
      const tool = extras(page).getByTestId('agent-card-consent-extra-tool')
      assert.equal(await tool.filter({ hasText: 'records_export' }).locator('input').isDisabled(), false)
      assert.equal(await tool.filter({ hasText: 'records_purge' }).locator('input').isDisabled(), true)
      assert.deepEqual(errors, [])
      await page.close()
    })

    await t.test('one Approve sends the request, then one merge per resource the extras touch, at the chained revision', async () => {
      const { page, errors } = await open(browser, origin, 'account-only')
      await expand(page)
      await operation(page, 'values.update').check()
      await extras(page).getByTestId('agent-card-consent-extra-grant').filter({ hasText: 'memories:read' }).locator('input').check()
      await extras(page).getByTestId('agent-card-consent-extra-tool').filter({ hasText: 'records_export' }).locator('input').check()
      // values.update needs sheets:read and sheets:write on one Google account: marked needed, never auto-ticked.
      const sheetsWrite = extras(page).getByTestId('agent-card-consent-extra-account-claim').filter({ hasText: 'sheets:write' })
      assert.ok((await sheetsWrite.innerText()).toLowerCase().includes('needed'))
      assert.equal(await sheetsWrite.locator('input').isChecked(), false)
      await sheetsWrite.locator('input').check()
      await page.getByRole('button', { name: 'Approve on this Card' }).click()
      await page.getByText('Approved. The agent').waitFor({ timeout: 10000 })
      const sent = await grants(page)
      assert.deepEqual(sent, [{
        client_id: CLIENT, resource: NAMED, claims: ['named_services:use', 'slides:read'], label: '', access_id: 'agent-0e150a469bf4edbd',
        account_scope: { google: { google_acct_1: ['slides:read', 'sheets:write'] } }, expected_card_revision: 7,
      }, {
        client_id: CLIENT, resource: MEMORIES, claims: ['memories:read'], label: '', access_id: 'agent-0e150a469bf4edbd', expected_card_revision: 8,
      }, {
        client_id: CLIENT, resource: SERVICE, claims: ['records:read'], label: '', access_id: 'agent-0e150a469bf4edbd',
        resource_operations: { [SERVICE]: ['records_list', 'records_export'] }, expected_card_revision: 9,
      }, {
        client_id: CLIENT, resource: NAMED, claims: ['named_services:use', 'slides:read'], label: '', access_id: 'agent-0e150a469bf4edbd',
        named_service_operations: { slides: ['object.list'], sheets: ['values.update'] }, expected_card_revision: 10,
      }])
      assert.ok(sent.every((grant) => !('replace' in grant)), 'the review only adds')
      assert.deepEqual(errors, [])
      await page.close()
    })

    await t.test('a permission no account holds: the button saves first, then opens that provider with the agent hand-off', async () => {
      const { page, errors } = await open(browser, origin, 'extras-missing')
      await expand(page)
      assert.equal(await page.getByTestId('agent-card-consent-extra-missing').count(), 0, 'nothing is missing before upsert is added')
      await operation(page, 'object.upsert').check()
      const missing = page.getByTestId('agent-card-consent-extra-missing')
      assert.ok((await missing.innerText()).includes('slides:write'))
      await missing.getByRole('button', { name: 'Save and request in Google' }).click()
      await page.waitForURL((url) => url.searchParams.get('tab') === 'delegated_to_kdcube', { timeout: 15000 })
      const url = new URL(page.url())
      assert.equal(url.searchParams.get('provider_id'), 'google')
      assert.equal(url.searchParams.get('claims'), 'slides:write')
      assert.equal(url.searchParams.get('account_id'), 'google_acct_1')
      assert.equal(url.searchParams.get('agent_client_id'), CLIENT)
      assert.equal(url.searchParams.get('agent_resource'), NAMED)
      const saved = grantsOf(await page.evaluate(() => window.__previous))
      assert.equal(saved.length, 2, 'the request and the added operation were saved before leaving')
      assert.deepEqual(saved[1].named_service_operations, { slides: ['object.list', 'object.upsert'] })
      assert.deepEqual(errors, [])
      await page.close()
    })

    await t.test('a refused save stays on the review and shows why', async () => {
      const { page } = await open(browser, origin, 'extras-missing-refused')
      await expand(page)
      await operation(page, 'object.upsert').check()
      await page.getByTestId('agent-card-consent-extra-missing').getByRole('button', { name: 'Save and request in Google' }).click()
      await page.getByRole('alert').waitFor({ timeout: 10000 })
      assert.ok((await page.getByRole('alert').innerText()).toLowerCase().includes("not allowed by this agent's control"))
      assert.equal(new URL(page.url()).searchParams.get('tab'), null, 'no navigation after a refusal')
      await page.close()
    })

    await t.test('the request is answered first: with its account not chosen, Approve stays disabled whatever else is ticked', async () => {
      const { page, errors } = await open(browser, origin, 'choose')
      await expand(page)
      await operation(page, 'values.update').check()
      const approve = page.getByRole('button', { name: 'Approve on this Card' })
      assert.equal(await approve.isDisabled(), true, 'two capable accounts and none chosen: the request is not answered')
      await page.getByTestId('agent-card-consent-accounts').locator('input[type=radio]').first().check()
      assert.equal(await approve.isDisabled(), false)
      await approve.click()
      await page.getByText('Approved. The agent').waitFor({ timeout: 10000 })
      const sent = await grants(page)
      assert.deepEqual(sent[0].account_scope, { google: { google_acct_1: ['slides:read'] } }, 'the request is in the first write')
      assert.deepEqual(sent[1].named_service_operations, { slides: ['object.list'], sheets: ['values.update'] })
      assert.deepEqual(errors, [])
      await page.close()
    })

    await t.test('no account of the provider: the request cannot be answered, so the button only connects and saves nothing', async () => {
      const { page, errors } = await open(browser, origin, 'extras-none')
      const missing = page.getByTestId('agent-card-consent-extra-missing')
      assert.ok((await missing.innerText()).includes('slides:read'), 'the request needs slides:read and no account exists')
      assert.ok((await missing.innerText()).includes('Nothing is saved before'))
      assert.equal(await page.getByRole('button', { name: 'Approve on this Card' }).isDisabled(), true)
      await missing.getByRole('button', { name: 'Connect a Google account' }).click()
      await page.waitForURL((url) => url.searchParams.get('tab') === 'delegated_to_kdcube', { timeout: 15000 })
      assert.equal(new URL(page.url()).searchParams.has('account_id'), false)
      assert.deepEqual(grantsOf(await page.evaluate(() => window.__previous)), [], 'nothing was selected, nothing written')
      assert.deepEqual(errors, [])
      await page.close()
    })
  } finally {
    await browser.close()
    await server.close()
  }
})
