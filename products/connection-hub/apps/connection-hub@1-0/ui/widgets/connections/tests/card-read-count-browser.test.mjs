// W587 release gate (operator 2026-10-06 14:50: "i asked not to refetch! i asked only when card is
// opened in connection hub (clicked on it for preview) and when edit is pressed. only then").
// The real panel and store in Chromium, the Hub answered by a counting bridge: a Control is read
// once to open it and once on Edit, and never again while it stays open (list reload, tab return,
// re-render, 30 s of time).
import assert from 'node:assert/strict'
import test from 'node:test'

import { launchBrowser, startFixtureServer } from './browser-fixture.mjs'

async function withBrowser(t, run) {
  const browser = await launchBrowser()
  if (!browser) {
    t.skip('no Chromium available; set PLAYWRIGHT_CHROMIUM_EXECUTABLE to run the Card read count')
    return
  }
  const server = await startFixtureServer()
  const origin = server.resolvedUrls.local[0]
  try {
    // Vite optimizes dependencies on the first load and reloads the page; a
    // warm-up load keeps that reload (and its duplicate-React errors) out of
    // the measured run.
    const warm = await browser.newPage()
    await warm.route('**/*', (route) => route.request().url().endsWith('/csrf')
      ? route.fulfill({ status: 200, contentType: 'application/json', body: '{"csrf_required":false}' })
      : route.request().url().startsWith(origin) ? route.continue() : route.abort())
    await warm.goto(`${origin}tests/fixtures/card-reads.html`, { waitUntil: 'load' })
    await warm.waitForTimeout(3000)
    await warm.close()
    await run(browser, origin)
  } finally {
    await server.close()
    await browser.close()
  }
}

for (const scenario of ['project', 'person']) {
  test(`a ${scenario} Control is read once on open and once on Edit, and never while it stays open`, async (t) => {
    await withBrowser(t, async (browser, origin) => {
      const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
      const errors = []
      page.on('pageerror', (error) => errors.push(String(error)))
      await page.route('**/*', async (route) => {
        const url = route.request().url()
        if (url.endsWith('/csrf')) {
          return route.fulfill({ status: 200, contentType: 'application/json', body: '{"csrf_required":false}' })
        }
        return url.startsWith(origin) ? route.continue() : route.abort()
      })
      await page.clock.install()
      await page.goto(`${origin}tests/fixtures/card-reads.html?scenario=${scenario}`, { waitUntil: 'domcontentloaded' })
      const reads = () => page.evaluate(() => window.__cardReads())
      const settle = async () => { await page.clock.runFor(1000); await page.waitForTimeout(100) }

      await page.waitForFunction(() => typeof window.__cardReads === 'function')
      for (let i = 0; i < 5; i += 1) await settle()
      const edit = page.getByRole('button', { name: 'Edit', exact: true })
      await edit.first().waitFor({ state: 'visible', timeout: 10000 })
      const log = [['open', await reads()]]

      await edit.first().click()
      for (let i = 0; i < 3; i += 1) await settle()
      log.push(['edit', await reads()])

      await page.evaluate(() => window.__reloadList())
      for (let i = 0; i < 3; i += 1) await settle()
      log.push(['list reload', await reads()])

      await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
      await settle()
      log.push(['tab return', await reads()])

      await page.setViewportSize({ width: 900, height: 900 })
      await settle()
      log.push(['re-render', await reads()])

      await page.clock.runFor(30000)
      await page.waitForTimeout(200)
      log.push(['idle 30 s', await reads()])

      const calls = await page.evaluate(() => window.__calls)
      t.diagnostic(`${scenario}: card reads per step ${JSON.stringify(log)}; all calls ${JSON.stringify(calls)}`)
      assert.deepEqual(errors, [])
      assert.deepEqual(log.map(([, count]) => count), [1, 2, 2, 2, 2, 2], `${scenario}: ${JSON.stringify(log)}`)
      await page.close()
    })
  })
}
