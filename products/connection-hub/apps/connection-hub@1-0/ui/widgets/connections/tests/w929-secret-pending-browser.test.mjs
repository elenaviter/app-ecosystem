// W929 (live 2026-10-23): a CLI secret set opened the consent page for one concrete secret, and the page stayed
// "Loading this capability from the active service catalog" with Once/Always disabled: the pending grant looked up
// its catalog row by string equality, and the catalog offers secrets only through its kdcube_secret selector row.
// Mounted: the real DelegatedAccessPanel and store in Chromium, the Hub a recording bridge
// (tests/fixtures/w929-secret-pending.tsx).
import assert from 'node:assert/strict'
import test from 'node:test'

import { launchBrowser, startFixtureServer } from './browser-fixture.mjs'

const SECRET = 'urn:kdcube:management:secret:demo-tenant:demo-project:bundle:connection-hub@1-0:connections.signing_key'
const WRITE = 'kdcube.management.secret.value.write'

test('the pending page for one concrete secret offers its operation and a Once/Always choice', async (t) => {
  const browser = await launchBrowser()
  if (!browser) {
    t.skip('no Chromium available; set PLAYWRIGHT_CHROMIUM_EXECUTABLE to run the mounted W929 check')
    return
  }
  const server = await startFixtureServer()
  const origin = server.resolvedUrls.local[0]
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  await page.route('**/*', (route) => route.request().url().endsWith('/csrf')
    ? route.fulfill({ status: 200, contentType: 'application/json', body: '{"csrf_required":false}' })
    : route.request().url().startsWith(origin) ? route.continue() : route.abort())
  const errors = []
  page.on('pageerror', (error) => errors.push(String(error)))
  try {
    // Vite optimizes dependencies on the first load and reloads; a warm-up keeps that out of the run.
    await page.goto(`${origin}tests/fixtures/w929-secret-pending.html`, { waitUntil: 'load' })
    await page.waitForTimeout(3000)
    await page.goto(`${origin}tests/fixtures/w929-secret-pending.html`, { waitUntil: 'domcontentloaded' })
    const operation = page.locator('label.namespace-operation input[type=checkbox]').first()
    await operation.waitFor({ state: 'visible', timeout: 15000 })
    if (!(await operation.isChecked())) await operation.check()
    const once = page.getByRole('button', { name: `${WRITE}: once` })
    await once.waitFor({ state: 'visible', timeout: 5000 })

    const text = await page.locator('body').innerText()
    assert.equal(text.includes('Loading this capability'), false, 'the capability resolves from the secret selector row')
    assert.ok(text.includes('Write a secret value'), 'the operation is offered by its catalog label')
    assert.ok(text.includes('Application connection-hub@1-0: connections.signing_key'), 'the concrete secret is named')

    assert.equal(await once.isEnabled(), true, 'Once is enabled')
    assert.equal(await page.getByRole('button', { name: `${WRITE}: always` }).isEnabled(), true, 'Always is enabled')
    await once.click()
    const allow = page.getByRole('button', { name: `Allow ${WRITE} once` })
    await allow.waitFor({ state: 'visible', timeout: 5000 })
    assert.equal(await allow.isEnabled(), true)
    await allow.click()
    await page.waitForFunction(() => window.__calls.some((call) => call.operation === 'delegated_agent_grant_create'), null, { timeout: 10000 })

    const grant = await page.evaluate(() => window.__calls.find((call) => call.operation === 'delegated_agent_grant_create').data)
    assert.equal(grant.resource, SECRET, 'the grant names the concrete secret, never the selector')
    assert.equal(grant.access_id, 'oauth-cli')
    assert.equal(grant.invocation_mode, 'once')
    assert.equal(grant.invocation_change_id, 'consent-1')
    assert.deepEqual(errors, [])
  } finally {
    await page.close()
    await server.close()
    await browser.close()
  }
})
