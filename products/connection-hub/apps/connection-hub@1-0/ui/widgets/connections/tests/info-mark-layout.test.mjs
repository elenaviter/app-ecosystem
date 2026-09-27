import assert from 'node:assert/strict'
import test from 'node:test'

import { bubblePlacement } from '../src/components/bubblePlacement.ts'
import { launchBrowser, startFixtureServer } from './browser-fixture.mjs'

// Operator, 2026-09-26 (W360): on the Card screen an operation's info bubble
// (`review.assign`) opened partly past the right edge of the viewport. Every
// bubble stays inside the viewport at any width.

test('a bubble that would cross the right edge moves left, never past the left margin', () => {
  const viewport = { width: 400, height: 800 }
  assert.deepEqual(bubblePlacement({ left: 20, top: 100, bottom: 114 }, { width: 320, height: 80 }, viewport), { left: 0, above: false })
  // At the right edge: the bubble ends 8px inside the viewport.
  assert.deepEqual(bubblePlacement({ left: 380, top: 100, bottom: 114 }, { width: 320, height: 80 }, viewport), { left: -308, above: false })
  // Wider than the viewport allows: it starts at the left margin.
  assert.deepEqual(bubblePlacement({ left: 200, top: 100, bottom: 114 }, { width: 500, height: 80 }, viewport), { left: -192, above: false })
})

test('a bubble with no room below opens above when there is room there', () => {
  const viewport = { width: 1200, height: 600 }
  assert.equal(bubblePlacement({ left: 20, top: 560, bottom: 574 }, { width: 320, height: 90 }, viewport).above, true)
  assert.equal(bubblePlacement({ left: 20, top: 40, bottom: 54 }, { width: 320, height: 90 }, viewport).above, false)
})

async function withBrowser(t, run) {
  const browser = await launchBrowser()
  if (!browser) {
    t.skip('no Chromium available; set PLAYWRIGHT_CHROMIUM_EXECUTABLE to run the info mark layout check')
    return
  }
  const server = await startFixtureServer()
  const origin = server.resolvedUrls.local[0]
  try {
    await run(browser, origin)
  } finally {
    await server.close()
    await browser.close()
  }
}

test('every info bubble stays inside the viewport at a narrow and a wide width', async (t) => {
  await withBrowser(t, async (browser, origin) => {
    for (const width of [360, 1440]) {
      const tab = await browser.newPage({ viewport: { width, height: 700 } })
      await tab.route('**/*', (route) => route.request().url().startsWith(origin) ? route.continue() : route.abort())
      await tab.goto(`${origin}tests/fixtures/info-mark.html`, { waitUntil: 'domcontentloaded' })
      await tab.waitForSelector('[data-row] .info-mark')
      for (const row of ['top-left', 'top-right', 'bottom-left', 'bottom-right']) {
        await tab.click(`[data-row="${row}"] .info-mark`)
        await tab.waitForSelector(`[data-row="${row}"] .info-mark__bubble`, { state: 'visible' })
        const box = await tab.evaluate((name) => {
          const bubble = document.querySelector(`[data-row="${name}"] .info-mark__bubble`)
          const mark = document.querySelector(`[data-row="${name}"] .info-mark`)
          return {
            bubble: bubble.getBoundingClientRect().toJSON(),
            mark: mark.getBoundingClientRect().toJSON(),
            viewport: { width: document.documentElement.clientWidth, height: window.innerHeight },
          }
        }, row)
        const where = `${width}px, ${row}`
        assert.ok(box.bubble.left >= 0, `${where}: bubble starts inside the left edge (${box.bubble.left})`)
        assert.ok(box.bubble.right <= box.viewport.width, `${where}: bubble ends inside the right edge (${box.bubble.right} > ${box.viewport.width})`)
        assert.ok(box.bubble.top >= 0, `${where}: bubble starts inside the top (${box.bubble.top})`)
        assert.ok(box.bubble.bottom <= box.viewport.height, `${where}: bubble ends inside the bottom (${box.bubble.bottom} > ${box.viewport.height})`)
        const opensAbove = row.startsWith('bottom')
        if (opensAbove) assert.ok(box.bubble.bottom <= box.mark.top, `${where}: with no room below, the bubble opens above the mark`)
        else assert.ok(box.bubble.top >= box.mark.bottom, `${where}: the bubble opens under the mark`)
        await tab.keyboard.press('Escape')
      }
      await tab.close()
    }
  })
})
