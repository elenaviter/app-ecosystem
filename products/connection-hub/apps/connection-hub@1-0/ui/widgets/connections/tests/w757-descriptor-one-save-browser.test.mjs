// The mounted descriptor Control Card editor sends one Card Save. Descriptor
// bundle props are only the seed; success and both refusal paths leave them alone.
import assert from 'node:assert/strict'
import test from 'node:test'

import { launchBrowser, startFixtureServer } from './browser-fixture.mjs'

test('descriptor Control Card Save is one request with no bundle props write', async (t) => {
  const browser = await launchBrowser()
  if (!browser) {
    t.skip('no Chromium available; set PLAYWRIGHT_CHROMIUM_EXECUTABLE')
    return
  }
  const server = await startFixtureServer()
  const origin = server.resolvedUrls.local[0]
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  let bundleWrites = 0
  await page.route('**/*', (route) => {
    if (route.request().url().endsWith('/csrf')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{"csrf_required":false}' })
    }
    if (route.request().url().includes('/admin/integrations/bundles/')
        && route.request().url().endsWith('/props')) {
      bundleWrites += 1
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true}' })
    }
    return route.request().url().startsWith(origin) ? route.continue() : route.abort()
  })
  try {
    await page.goto(`${origin}tests/fixtures/w757-descriptor-save.html`, { waitUntil: 'load' })
    await page.waitForTimeout(2500)
    for (const answer of ['ok', 'refused', 'stale']) {
      bundleWrites = 0
      await page.goto(`${origin}tests/fixtures/w757-descriptor-save.html?answer=${answer}`,
        { waitUntil: 'domcontentloaded' })
      const edit = page.getByRole('button', { name: 'Edit', exact: true }).first()
      await edit.waitFor({ state: 'visible', timeout: 15000 })
      await edit.click()
      await page.waitForFunction(() => window.__calls.length > 1, null, { timeout: 10000 })
      const newTool = page.locator('section[aria-label="KDCube administrator preset"] label[title="tool.new"]')
      await newTool.waitFor({ state: 'visible', timeout: 10000 })
      await newTool.click()
      assert.equal(await newTool.locator('input').isChecked(), true)
      await page.getByRole('button', { name: 'Save', exact: true }).click()
      await page.waitForFunction(() => window.__calls.some((call) => call.operation === 'delegated_access_update'),
        { timeout: 10000 })
      const calls = await page.evaluate(() => window.__calls)
      const writes = calls.filter((call) => call.operation === 'delegated_access_update')
      assert.equal(writes.length, 1, `${answer}: one Card Save`)
      assert.equal(bundleWrites, 0, `${answer}: bundle props unchanged`)
      assert.equal(writes[0].data.access_id, 'descriptor-control-1')
      assert.equal(writes[0].data.expected_card_revision, 4)
      assert.equal(writes[0].data.expected_catalog_version, 'cat-1')
      assert.deepEqual(
        [...writes[0].data.properties['kdcube.agent_capability_defaults'].capabilities.tools].sort(),
        ['tool.new', 'tool.old'],
      )
      if (answer !== 'ok') {
        assert.equal(await page.getByRole('button', { name: 'Save', exact: true }).isVisible(), true)
        assert.equal(await newTool.locator('input').isChecked(), true)
      }
    }
  } finally {
    await page.close()
    await server.close()
    await browser.close()
  }
})
