// W918: a Card's personal MCP servers change only by the Card's owner (the Hub's owner_only_connectors rule).
// Mounted: the real DelegatedAccessPanel in Chromium (tests/fixtures/w918-connector-owner-only.tsx). A viewer who
// is not the owner, and has no copy of the owner's connector in their own catalog, sees the connector row
// disabled with the owner-only line, and can still remove the whole connector and undo that; the owner's own
// editor is unchanged.
import assert from 'node:assert/strict'
import test from 'node:test'

import { launchBrowser, startFixtureServer } from './browser-fixture.mjs'

const CONNECTOR = 'urn:connection-hub:remote-mcp:mcp_aaaaaaaaaaaaaaaaaaaaaaaa'
const NOTE = "Only the Card's owner changes these."

const BOARD = 'https://board.example.test/mcp/problem_board'
const saves = (page) => page.evaluate(() => window.__calls
  .filter((call) => call.operation === 'delegated_access_update').map((call) => call.data))

/** Ticks one more board tool (a change this viewer may make), Saves, and returns the one Save request. */
async function saveWithBoardChange(page) {
  await page.evaluate(() => document.querySelectorAll('details').forEach((element) => { element.open = true }))
  await page.locator('.tool-choice label.grant-chip[title="plan.item.list"]').click()
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  await page.waitForFunction(() => window.__calls.some((call) => call.operation === 'delegated_access_update'),
    null, { timeout: 10000 })
  const sent = await saves(page)
  assert.equal(sent.length, 1, 'exactly one Save request')
  return sent[0]
}

async function withEditor(t, viewer, run, { link = false } = {}) {
  const browser = await launchBrowser()
  if (!browser) {
    t.skip('no Chromium available; set PLAYWRIGHT_CHROMIUM_EXECUTABLE to run the mounted W918 check')
    return
  }
  const server = await startFixtureServer()
  const origin = server.resolvedUrls.local[0]
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  const errors = []
  page.on('pageerror', (error) => errors.push(String(error)))
  await page.route('**/*', (route) => route.request().url().endsWith('/csrf')
    ? route.fulfill({ status: 200, contentType: 'application/json', body: '{"csrf_required":false}' })
    : route.request().url().startsWith(origin) ? route.continue() : route.abort())
  try {
    await page.goto(`${origin}tests/fixtures/w918-connector-owner-only.html?viewer=${viewer}${link ? '&link=1' : ''}`,
      { waitUntil: 'domcontentloaded' })
    if (!link) {
      // A guided edit link opens the editor itself; otherwise open it.
      const edit = page.getByRole('button', { name: 'Edit', exact: true })
      await edit.first().waitFor({ state: 'visible', timeout: 15000 })
      await edit.first().click()
    }
    const row = page.locator(`details.resource-edit-section[data-resource="${CONNECTOR}"]`)
    await row.waitFor({ state: 'attached', timeout: 15000 })
    // Open the row (an existing resource starts collapsed).
    if (!(await row.evaluate((element) => element.open))) await row.locator('summary').first().click()
    await run({ page, row })
    assert.deepEqual(errors, [])
  } finally {
    await page.close()
    await server.close()
    await browser.close()
  }
}

test("a non-owner sees the owner's connector read-only with the line, and can remove and undo it", async (t) => {
  await withEditor(t, 'other', async ({ page, row }) => {
    assert.equal(await row.locator('fieldset.resource-edit-section__body').evaluate((element) => element.disabled), true)
    assert.equal((await row.locator('.resource-edit-section__owner-only').innerText()).trim(), NOTE)
    const controls = row.locator('fieldset.resource-edit-section__body').locator('input, button, select, textarea')
    for (let index = 0; index < await controls.count(); index += 1) {
      assert.equal(await controls.nth(index).isDisabled(), true, `control ${index} is disabled`)
    }
    const remove = row.getByRole('button', { name: 'Remove from card' })
    assert.equal(await remove.isEnabled(), true, 'whole removal stays available')
    await remove.click()
    const undo = page.getByRole('button', { name: /Undo/ })
    await undo.first().waitFor({ state: 'visible', timeout: 5000 })
    await undo.first().click()
    await page.locator(`details.resource-edit-section[data-resource="${CONNECTOR}"]`).waitFor({ state: 'attached' })
  })
})

test("the owner's own editor is unchanged", async (t) => {
  await withEditor(t, 'owner', async ({ row }) => {
    assert.equal(await row.locator('fieldset.resource-edit-section__body').evaluate((element) => element.disabled), false)
    assert.equal(await row.locator('.resource-edit-section__owner-only').count(), 0)
  })
})

test("a non-owner's Save of another change keeps the owner's connector exactly as held", async (t) => {
  await withEditor(t, 'other', async ({ page }) => {
    const body = await saveWithBoardChange(page)
    assert.deepEqual(body.resource_grants[CONNECTOR], ['external_mcp:use'], 'the connector grant is kept')
    assert.deepEqual(body.resource_operations[CONNECTOR], ['search'], 'the connector tools are kept')
    assert.deepEqual([...body.resource_operations[BOARD]].sort(), ['plan.item.get', 'plan.item.list'])
  })
})

test("a guided link naming the owner's connector seeds nothing for a non-owner", async (t) => {
  await withEditor(t, 'other', async ({ page, row }) => {
    assert.equal(await row.locator('.resource-edit-section__owner-only').count(), 1)
    const body = await saveWithBoardChange(page)
    assert.deepEqual(body.resource_operations[CONNECTOR], ['search'], 'the linked fetch was not added')
  }, { link: true })
})

test("a non-owner's unchanged Save goes through and keeps the owner's connector as held", async (t) => {
  await withEditor(t, 'other', async ({ page }) => {
    const save = page.getByRole('button', { name: 'Save', exact: true })
    assert.equal(await save.isEnabled(), true, 'a Save with no change is still offered (the whole Card travels)')
    await save.click()
    await page.waitForFunction(() => window.__calls.some((call) => call.operation === 'delegated_access_update'),
      null, { timeout: 10000 })
    const sent = await saves(page)
    assert.equal(sent.length, 1, 'exactly one Save request')
    assert.deepEqual(sent[0].resource_grants[CONNECTOR], ['external_mcp:use'], 'the connector grant is kept')
    assert.deepEqual(sent[0].resource_operations[CONNECTOR], ['search'], 'the connector tools are kept')
    assert.deepEqual(sent[0].resource_operations[BOARD], ['plan.item.get'], 'nothing else changed')
  })
})
