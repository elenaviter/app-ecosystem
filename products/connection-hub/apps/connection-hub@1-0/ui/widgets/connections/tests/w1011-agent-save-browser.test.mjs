// The real panel/store and Chromium, with a recording Hub bridge. This proves
// the submitted request and visible Save outcome, not the backend transaction.
import assert from 'node:assert/strict'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'

import { launchBrowser, startFixtureServer } from './browser-fixture.mjs'

test('a hosted-agent permission-only Save omits properties and closes the real editor', async (t) => {
  const browser = await launchBrowser()
  if (!browser) {
    t.skip('no Chromium available; set PLAYWRIGHT_CHROMIUM_EXECUTABLE; this skip does not qualify the UI')
    return
  }
  const server = await startFixtureServer()
  const origin = server.resolvedUrls.local[0]
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  const screenshot = process.env.CONNECTION_HUB_TEST_SCREENSHOT_PATH
    || join(mkdtempSync(join(tmpdir(), 'hosted-agent-save-')), 'permission-save.png')
  const errors = []
  page.on('pageerror', (error) => errors.push(String(error)))
  await page.route('**/*', (route) => route.request().url().endsWith('/csrf')
    ? route.fulfill({ status: 200, contentType: 'application/json', body: '{"csrf_required":false}' })
    : route.request().url().startsWith(origin) ? route.continue() : route.abort())
  try {
    const url = `${origin}tests/fixtures/w755-save.html?card=agent`
    await page.goto(url, { waitUntil: 'load' })
    await page.waitForTimeout(2500) // one Vite dependency warm-up/reload
    await page.goto(url, { waitUntil: 'domcontentloaded' })
    const edit = page.getByRole('button', { name: 'Edit', exact: true }).first()
    await edit.waitFor({ state: 'visible', timeout: 15000 })
    await edit.click()
    const write = page.locator('.tool-choice label.grant-chip[title="write"]')
    await write.waitFor({ state: 'attached', timeout: 15000 })
    await page.evaluate(() => document.querySelectorAll('details').forEach((element) => { element.open = true }))
    await write.click()
    assert.equal(await write.locator('input').isChecked(), false)
    await page.getByRole('button', { name: 'Save', exact: true }).click()
    await page.waitForFunction(() => window.__calls.some((call) => call.operation === 'delegated_access_update'),
      null, { timeout: 10000 })
    const saves = await page.evaluate(() => window.__calls
      .filter((call) => call.operation === 'delegated_access_update').map((call) => call.data))
    assert.equal(saves.length, 1)
    assert.equal('properties' in saves[0], false)
    assert.equal(saves[0].expected_card_revision, 4)
    assert.equal(saves[0].expected_catalog_version, 'cat-1')
    assert.deepEqual(saves[0].resource_operations['https://svc.example.test/mcp'], ['read'])
    await page.getByRole('button', { name: 'Save', exact: true }).waitFor({ state: 'hidden', timeout: 10000 })
    assert.deepEqual(await page.evaluate(() => window.__saved.resource_operations['https://svc.example.test/mcp']), ['read'])
    assert.deepEqual(errors, [])
    await page.screenshot({ path: screenshot, fullPage: true })
    t.diagnostic(`Hosted-agent Save screenshot: ${screenshot}`)
  } finally {
    await page.close()
    await server.close()
    await browser.close()
  }
})
