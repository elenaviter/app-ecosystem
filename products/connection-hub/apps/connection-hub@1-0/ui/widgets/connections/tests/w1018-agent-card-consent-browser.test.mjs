// W1018 A (operator, 2026-11-07: "W1018 - A"): a hosted agent's consent opens on its own Agent Card.
// Mounted: the real DelegatedAccessPanel and store in Chromium, the Hub a recording bridge
// (tests/fixtures/w1018-agent-card-consent.tsx). W1018 rows 3/4: the account-binding link opened a pending pane
// with nothing preselected and no account section, so the consent could not write account_scope.
import assert from 'node:assert/strict'
import test from 'node:test'

import { launchBrowser, startFixtureServer } from './browser-fixture.mjs'

const NAMED = '*/api/integrations/bundles/*/*/kdcube-services@1-0/public/mcp/named_services*'
const SHOTS = process.env.W1018_SHOT_DIR || ''

async function open(browser, origin, variant, width = 1280, query = '') {
  const page = await browser.newPage({ viewport: { width, height: 1400 } })
  await page.route('**/*', (route) => route.request().url().endsWith('/csrf')
    ? route.fulfill({ status: 200, contentType: 'application/json', body: '{"csrf_required":false}' })
    : route.request().url().startsWith(origin) ? route.continue() : route.abort())
  const errors = []
  page.on('pageerror', (error) => errors.push(String(error)))
  const url = `${origin}tests/fixtures/w1018-agent-card-consent.html?variant=${variant}${query}`
  // Vite optimizes dependencies on the first load and reloads; a warm-up keeps that out of the run.
  await page.goto(url, { waitUntil: 'load' })
  await page.waitForTimeout(2000)
  await page.goto(url, { waitUntil: 'domcontentloaded' })
  return { page, errors }
}

async function grants(page) {
  return page.evaluate(() => window.__calls.filter((call) => call.operation === 'delegated_agent_grant_create').map((call) => call.data))
}

async function shot(page, name) {
  if (!SHOTS) return
  await page.locator('[data-testid=agent-card-consent], [data-testid=agent-card-consent-refused]').first()
    .screenshot({ path: `${SHOTS}/w1018a-${name}.png` })
}

test('W1018 A: the agent Card consent review', async (t) => {
  const browser = await launchBrowser()
  if (!browser) {
    t.skip('no Chromium available; set PLAYWRIGHT_CHROMIUM_EXECUTABLE to run the mounted W1018 check')
    return
  }
  const server = await startFixtureServer()
  const origin = server.resolvedUrls.local[0]
  try {
    await t.test('the account-only link opens the agent Card with the sole capable account proposed; one Approve writes account_scope', async () => {
      const { page, errors } = await open(browser, origin, 'account-only')
      const review = page.getByTestId('agent-card-consent')
      await review.waitFor({ timeout: 15000 })
      const text = await review.innerText()
      assert.ok(text.includes('agent-0e150a469bf4edbd') && text.includes('revision 7'), 'the Card and its revision are shown')
      assert.ok(text.includes('Descriptor ceiling') || text.includes('Control:'), 'its Control is shown')
      assert.equal(await page.getByText('An agent is asking for your permission', { exact: true }).count(), 0, 'no pending pane beside it')
      const box = page.getByTestId('agent-card-consent-accounts').locator('input[type=checkbox]')
      assert.equal(await box.count(), 1)
      assert.equal(await box.isChecked(), true, 'the only capable account is proposed')
      await shot(page, 'account-only-desktop')
      await page.getByRole('button', { name: 'Approve on this Card' }).click()
      await page.getByText('Approved. The agent').waitFor({ timeout: 10000 })
      const sent = await grants(page)
      assert.equal(sent.length, 1, 'one grant, no later Card Save')
      assert.deepEqual(sent[0], {
        client_id: 'kdcube-agent:workspace@2026-03-31-13-36:main', resource: NAMED,
        claims: ['named_services:use', 'slides:read'], label: '', access_id: 'agent-0e150a469bf4edbd',
        account_scope: { google: { google_acct_1: ['slides:read'] } }, expected_card_revision: 7,
      })
      assert.equal(await page.evaluate(() => window.__calls.filter((c) => c.operation === 'delegated_access_update').length), 0)
      assert.deepEqual(errors, [])
      await page.close()
    })

    await t.test('the SDK link entrance: the same review from URL params alone (empty claims, provider named)', async () => {
      const query = '&' + new URLSearchParams({
        agent_card_consent: '1', pending_agent_grant: '1', agent_client_id: 'kdcube-agent:workspace@2026-03-31-13-36:main',
        resource: NAMED, claims: '', account_claim: 'slides:read', account_provider_id: 'google',
      }).toString()
      const { page, errors } = await open(browser, origin, 'url', 1280, query)
      await page.getByTestId('agent-card-consent').waitFor({ timeout: 15000 })
      assert.equal(await page.getByText('An agent is asking for your permission', { exact: true }).count(), 0)
      assert.equal(await page.getByTestId('agent-card-consent-accounts').locator('input[type=checkbox]').isChecked(), true)
      await page.getByRole('button', { name: 'Approve on this Card' }).click()
      await page.getByText('Approved. The agent').waitFor({ timeout: 10000 })
      assert.deepEqual((await grants(page))[0].account_scope, { google: { google_acct_1: ['slides:read'] } })
      assert.deepEqual(errors, [])
      await page.close()
    })

    await t.test('several capable accounts: none is proposed and Approve waits for the owner\'s choice', async () => {
      const { page, errors } = await open(browser, origin, 'choose')
      const review = page.getByTestId('agent-card-consent')
      await review.waitFor({ timeout: 15000 })
      assert.ok((await review.innerText()).includes('Several of your accounts can do this'))
      const boxes = page.getByTestId('agent-card-consent-accounts').locator('input[type=checkbox]')
      assert.equal(await boxes.count(), 2)
      assert.equal(await boxes.nth(0).isChecked(), false)
      assert.equal(await boxes.nth(1).isChecked(), false)
      const approve = page.getByRole('button', { name: 'Approve on this Card' })
      assert.equal(await approve.isEnabled(), false)
      await boxes.nth(1).check()
      assert.equal(await approve.isEnabled(), true)
      await shot(page, 'choose-desktop')
      await approve.click()
      await page.getByText('Approved. The agent').waitFor({ timeout: 10000 })
      assert.deepEqual((await grants(page))[0].account_scope, { google: { google_acct_2: ['slides:read'] } })
      assert.deepEqual(errors, [])
      await page.close()
    })

    await t.test('an operation inside the Control is offered ticked and granted with its account claim', async () => {
      const { page, errors } = await open(browser, origin, 'operation')
      await page.getByTestId('agent-card-consent').waitFor({ timeout: 15000 })
      const op = page.getByTestId('agent-card-consent-operation').locator('input[type=checkbox]')
      assert.equal(await op.isChecked(), true)
      assert.equal(await op.isEnabled(), true)
      await page.getByRole('button', { name: 'Approve on this Card' }).click()
      await page.getByText('Approved. The agent').waitFor({ timeout: 10000 })
      const [grant] = await grants(page)
      assert.deepEqual(grant.named_service_operations, { slides: ['object.list', 'object.upsert'] })
      // slides:write is the operation's connected-account claim (claims_by_operation), not a door grant: it goes to the account.
      assert.deepEqual(grant.claims, ['named_services:use', 'slides:read'])
      assert.deepEqual(grant.account_scope, { google: { google_acct_1: ['slides:write'] } })
      assert.deepEqual(errors, [])
      await page.close()
    })

    await t.test('an operation outside the agent\'s Control is shown unavailable and never preselected or sent', async () => {
      const { page, errors } = await open(browser, origin, 'outside')
      await page.getByTestId('agent-card-consent').waitFor({ timeout: 15000 })
      const section = page.getByTestId('agent-card-consent-operation')
      assert.match(await section.innerText(), /not in this agent's Control/i)
      const op = section.locator('input[type=checkbox]')
      assert.equal(await op.isChecked(), false)
      assert.equal(await op.isEnabled(), false)
      await shot(page, 'outside-desktop')
      await page.getByRole('button', { name: 'Approve on this Card' }).click()
      await page.getByText('Approved. The agent').waitFor({ timeout: 10000 })
      const [grant] = await grants(page)
      assert.equal('named_service_operations' in grant, false, 'the out-of-Control operation is not sent')
      assert.deepEqual(grant.account_scope, { google: { google_acct_1: ['slides:read'] } })
      assert.deepEqual(errors, [])
      await page.close()
    })

    await t.test('the old pending pane does not preselect an operation outside the agent\'s Control either', async () => {
      const { page, errors } = await open(browser, origin, 'pending-outside')
      await page.getByText('An agent is asking for your permission', { exact: true }).waitFor({ timeout: 15000 })
      await page.waitForTimeout(500)
      const label = page.locator('label.namespace-operation').filter({ hasText: 'provider.capabilities' }).first()
      assert.match(await label.innerText(), /not in this agent's Control/i)
      const box = label.locator('input[type=checkbox]')
      assert.equal(await box.isChecked(), false)
      assert.equal(await box.isEnabled(), false)
      if (SHOTS) await page.screenshot({ path: `${SHOTS}/w1018a-pending-outside-desktop.png`, fullPage: false })
      assert.deepEqual(errors, [])
      await page.close()
    })

    await t.test('a refusal names what is outside the Control from the bounded details', async () => {
      const { page, errors } = await open(browser, origin, 'refused')
      await page.getByTestId('agent-card-consent').waitFor({ timeout: 15000 })
      await page.getByRole('button', { name: 'Approve on this Card' }).click()
      const alert = page.getByTestId('agent-card-consent').getByRole('alert')
      await alert.waitFor({ timeout: 10000 })
      assert.equal(await alert.innerText(), "Not allowed by this agent's Control: slides.object.upsert on Named services.")
      await shot(page, 'refused-desktop')
      assert.deepEqual(errors, [])
      await page.close()
    })

    await t.test('a link naming another agent\'s Card opens nothing', async () => {
      const { page, errors } = await open(browser, origin, 'mismatch')
      const refused = page.getByTestId('agent-card-consent-refused')
      await refused.waitFor({ timeout: 15000 })
      assert.ok((await refused.innerText()).includes('You hold no active Card') || (await refused.innerText()).includes('does not belong'))
      assert.equal(await page.getByTestId('agent-card-consent').count(), 0)
      assert.deepEqual(errors, [])
      await page.close()
    })

    await t.test('reopened after approval, the Card shows the account as already allowed and nothing to approve', async () => {
      // The bridge keeps the committed scope for the page's lifetime: reload the review on the same page.
      const { page, errors } = await open(browser, origin, 'provider')
      await page.getByTestId('agent-card-consent').waitFor({ timeout: 15000 })
      await page.getByRole('button', { name: 'Approve on this Card' }).click()
      await page.getByText('Approved. The agent').waitFor({ timeout: 10000 })
      // The approved Card reloads at revision 8 with the binding: a fresh review (keyed by revision) shows it held.
      await page.waitForFunction(() => document.body.innerText.includes('revision 8'), null, { timeout: 10000 })
      const text = await page.getByTestId('agent-card-consent').innerText()
      assert.match(text, /already allowed/i)
      assert.equal(await page.getByRole('button', { name: 'Approve on this Card' }).isEnabled(), false)
      assert.deepEqual(errors, [])
      await page.close()
    })

    await t.test('phone width: the review fits without page-wide horizontal scroll', async () => {
      for (const variant of ['account-only', 'choose', 'outside']) {
        const { page, errors } = await open(browser, origin, variant, 390)
        await page.getByTestId('agent-card-consent').waitFor({ timeout: 15000 })
        const width = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth }))
        assert.ok(width.sw <= width.cw, `${variant}: page ${width.sw} > ${width.cw}`)
        await shot(page, `${variant}-phone`)
        assert.deepEqual(errors, [])
        await page.close()
      }
    })
  } finally {
    await browser.close()
    await server.close()
  }
})
