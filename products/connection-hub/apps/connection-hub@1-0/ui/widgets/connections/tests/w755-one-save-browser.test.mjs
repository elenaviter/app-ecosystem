// W755 T16 (operator, 2026-10-11: "WHY ITS NOT A PART"; "the new card yes must be sent to save"; the Save
// contract of 21:03Z: "the version of the card as it was when we shown it ... and the contents of the card (as
// is) as filled"). Mounted: the real DelegatedAccessPanel and store in Chromium, the Hub a recording bridge
// (tests/fixtures/w755-save.tsx). Every kind of Save is ONE delegated_access_update carrying the whole Card,
// both render-time pins and the policy of each added operation; no per-operation grant is ever sent; every
// refusal keeps the draft.
import assert from 'node:assert/strict'
import test from 'node:test'

import { launchBrowser, startFixtureServer } from './browser-fixture.mjs'

const RES = 'https://svc.example.test/mcp'
const STALE = 'This Card or its service catalog changed on the server since you opened it'

async function withBrowser(t, run) {
  const browser = await launchBrowser()
  if (!browser) {
    t.skip('no Chromium available; set PLAYWRIGHT_CHROMIUM_EXECUTABLE to run the mounted W755 check')
    return
  }
  const server = await startFixtureServer()
  const origin = server.resolvedUrls.local[0]
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  await page.route('**/*', (route) => route.request().url().endsWith('/csrf')
    ? route.fulfill({ status: 200, contentType: 'application/json', body: '{"csrf_required":false}' })
    : route.request().url().startsWith(origin) ? route.continue() : route.abort())
  try {
    // Vite optimizes dependencies on the first load and reloads; a warm-up keeps that out of the run.
    await page.goto(`${origin}tests/fixtures/w755-save.html`, { waitUntil: 'load' })
    await page.waitForTimeout(3000)
    await run(page, origin)
  } finally {
    await page.close()
    await server.close()
    await browser.close()
  }
}

/** Opens the fixture with one Save answer and the Card's editor, every section unfolded. */
async function openEditor(page, origin, answer) {
  await page.goto(`${origin}tests/fixtures/w755-save.html?answer=${answer}`, { waitUntil: 'domcontentloaded' })
  const edit = page.getByRole('button', { name: 'Edit', exact: true })
  await edit.first().waitFor({ state: 'visible', timeout: 15000 })
  await edit.first().click()
  await page.locator('.tool-choice').first().waitFor({ state: 'attached', timeout: 15000 })
  await page.evaluate(() => document.querySelectorAll('details').forEach((element) => { element.open = true }))
}

const tool = (page, operation) => page.locator(`.tool-choice label.grant-chip[title="${operation}"]`)
const toolTicked = (page, operation) => tool(page, operation).locator('input').isChecked()
const saves = (page) => page.evaluate(() => window.__calls.filter((call) => call.operation === 'delegated_access_update').map((call) => call.data))
const grants = (page) => page.evaluate(() => window.__calls.filter((call) => /grant_create|grant_resume/.test(call.operation)).length)
const shown = (page) => page.evaluate(() => [...document.querySelectorAll('[role=alert], [role=status]')].map((element) => element.innerText).join(' | '))
const editorOpen = (page) => page.getByRole('button', { name: 'Save', exact: true }).isVisible()

async function save(page) {
  const before = (await saves(page)).length
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  await page.waitForFunction((count) => window.__calls.filter((call) => call.operation === 'delegated_access_update').length > count, before, { timeout: 10000 })
  await page.waitForTimeout(400)
}

/** The request is the whole Card, as rendered: both pins and every field, whatever changed. */
function assertWholeCard(body, operations) {
  assert.equal(body.access_id, 'card-1')
  assert.equal(body.expected_card_revision, 4, 'the Card revision the editor was opened on')
  assert.equal(body.expected_catalog_version, 'cat-1', 'the catalog version the editor rendered with')
  assert.deepEqual(body.resource_grants, { [RES]: ['svc:read'] })
  assert.deepEqual([...body.resource_operations[RES]].sort(), [...operations].sort())
  assert.equal(typeof body.label, 'string')
}

test('add only: one request with the whole Card and the chosen Once; the editor closes', async (t) => {
  await withBrowser(t, async (page, origin) => {
    const errors = []
    page.on('pageerror', (error) => errors.push(String(error)))
    await openEditor(page, origin, 'ok')
    await tool(page, 'delete').click()
    await page.getByRole('button', { name: 'delete: once' }).click()
    await save(page)
    const [body, ...more] = await saves(page)
    assert.deepEqual(more, [], 'exactly one Save request')
    assertWholeCard(body, ['read', 'write', 'delete'])
    assert.deepEqual(body.invocation_modes, { [RES]: { delete: 'once' } })
    assert.equal(await grants(page), 0, 'no per-operation grant')
    assert.equal(await editorOpen(page), false)
    assert.deepEqual(errors, [])
  })
})

test('an added operation without Once or Every time sends nothing until it is chosen', async (t) => {
  await withBrowser(t, async (page, origin) => {
    await openEditor(page, origin, 'ok')
    await tool(page, 'delete').click()
    const button = page.getByRole('button', { name: 'Save', exact: true })
    assert.equal(await button.isDisabled(), true, 'Save waits for the choice')
    assert.equal(await toolTicked(page, 'delete'), true, 'the draft is kept')
    await page.getByRole('button', { name: 'delete: once' }).click()
    assert.equal(await button.isDisabled(), false)
    assert.deepEqual(await saves(page), [], 'nothing was sent before the choice')
  })
})

test('remove only: one request without the removed operation and without modes', async (t) => {
  await withBrowser(t, async (page, origin) => {
    await openEditor(page, origin, 'ok')
    await tool(page, 'write').click()
    await save(page)
    const [body, ...more] = await saves(page)
    assert.deepEqual(more, [])
    assertWholeCard(body, ['read'])
    assert.equal('invocation_modes' in body, false)
    assert.equal(await grants(page), 0)
  })
})

test('name only: one request with the whole Card and the new name', async (t) => {
  await withBrowser(t, async (page, origin) => {
    await openEditor(page, origin, 'ok')
    const name = page.locator('.card-editor input[type=text]').first()
    await name.fill('Build token (renamed)')
    await save(page)
    const [body, ...more] = await saves(page)
    assert.deepEqual(more, [])
    assertWholeCard(body, ['read', 'write'])
    assert.equal(body.label, 'Build token (renamed)')
    assert.equal('invocation_modes' in body, false)
  })
})

test('mixed: add with Every time, remove another, rename: still one request', async (t) => {
  await withBrowser(t, async (page, origin) => {
    await openEditor(page, origin, 'ok')
    await tool(page, 'write').click()
    await tool(page, 'delete').click()
    await page.getByRole('button', { name: 'delete: always' }).click()
    await page.locator('.card-editor input[type=text]').first().fill('Mixed')
    await save(page)
    const [body, ...more] = await saves(page)
    assert.deepEqual(more, [])
    assertWholeCard(body, ['read', 'delete'])
    assert.deepEqual(body.invocation_modes, { [RES]: { delete: 'always' } })
    assert.equal(body.label, 'Mixed')
    assert.equal(await grants(page), 0)
  })
})

test('unchanged (equal revision, same content): "Nothing to save", the editor stays open', async (t) => {
  await withBrowser(t, async (page, origin) => {
    await openEditor(page, origin, 'unchanged')
    await save(page)
    assert.equal((await saves(page)).length, 1)
    assert.match(await shown(page), /Nothing to save: this Card already holds these choices\./)
    assert.equal(await editorOpen(page), true)
  })
})

for (const [answer, expected] of [
  ['refused', /Refused by the project: card_plan_selection_invalid\./],
  ['mode_required', /choose Once or Every time for each operation you added\. Your draft is kept\./],
  ['stale', new RegExp(STALE)], // T17: a newer Card revision on the server
  ['catalog', new RegExp(STALE)], // T18: the catalog version is not the newest
]) {
  test(`a refusal (${answer}) names why, keeps the draft, and sent one request`, async (t) => {
    await withBrowser(t, async (page, origin) => {
      await openEditor(page, origin, answer)
      await tool(page, 'delete').click()
      await page.getByRole('button', { name: 'delete: once' }).click()
      await save(page)
      assert.equal((await saves(page)).length, 1)
      assert.match(await shown(page), expected)
      assert.equal(await editorOpen(page), true)
      assert.equal(await toolTicked(page, 'delete'), true, 'the draft is kept')
      if (answer === 'stale' || answer === 'catalog') {
        assert.equal(await page.getByRole('button', { name: 'Reload this Card' }).isVisible(), true)
      }
    })
  })
}

test('a lost answer: Save again meets the stale refusal and Reload (operator, 21:03Z), never a second write', async (t) => {
  await withBrowser(t, async (page, origin) => {
    await openEditor(page, origin, 'lost')
    await tool(page, 'delete').click()
    await page.getByRole('button', { name: 'delete: once' }).click()
    await save(page)
    assert.match(await shown(page), /Save was not applied: .*Failed to fetch/)
    assert.equal(await toolTicked(page, 'delete'), true)
    await save(page)
    const [first, second, ...more] = await saves(page)
    assert.deepEqual(more, [])
    assert.deepEqual(second, first, 'the same request, the same revision fence')
    assert.match(await shown(page), new RegExp(STALE))
    assert.equal(await page.getByRole('button', { name: 'Reload this Card' }).isVisible(), true)
    assert.equal(await grants(page), 0)
  })
})
