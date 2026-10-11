// W739 (operator, on W731, 2026-10-11: "Cards hide project.control.get since it's a membership read no Card
// decides << we still show this (simply disabled)", "ie.. we must", "because for other permisisons that also
// card does not decide we still show them"). Mounted: the real DelegatedAccessPanel and store in Chromium,
// the Hub answered by a fake bridge with Infra's W731 catalog (tests/fixtures/w731-pb-operations.json). In a
// person's Control and My Card editor, the 49 outer operations no Card decides are present, disabled and
// "Set by the application"; the 45 the Card decides stay editable; on the Control Card the six role-only
// named aliases are shown disabled too.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { launchBrowser, startFixtureServer } from './browser-fixture.mjs'

const table = JSON.parse(readFileSync(new URL('./fixtures/w731-pb-operations.json', import.meta.url), 'utf8'))
const UNDECIDED = [...table.absent, ...table.role_only].sort()
const DECLARED = [...table.declared].sort()

async function withBrowser(t, run) {
  const browser = await launchBrowser()
  if (!browser) {
    t.skip('no Chromium available; set PLAYWRIGHT_CHROMIUM_EXECUTABLE to run the mounted W739 check')
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
    await page.goto(`${origin}tests/fixtures/w739-person-ops.html`, { waitUntil: 'load' })
    await page.waitForTimeout(3000)
    await run(page, origin)
  } finally {
    await page.close()
    await server.close()
    await browser.close()
  }
}

/** Every rendered outer and named row of the editor: its id, checkbox state and managed note. */
function readRows() {
  const row = (element, name) => {
    const box = element.querySelector('input[type="checkbox"]')
    return { name, disabled: Boolean(box?.disabled), checked: Boolean(box?.checked),
      managed: Boolean(element.querySelector('.grant-chip-managed')?.textContent?.includes('Set by the application')) }
  }
  const outer = [...document.querySelectorAll('.resource-operations .outer-operation-editor')].map((element) => row(
    element, (element.querySelector('code.operation-id') || element.querySelector('.operation-name span'))?.textContent?.trim()))
  const named = [...document.querySelectorAll('.namespace-operation')].map((element) => row(
    element, (element.querySelector('code.operation-id') || element.querySelector('strong'))?.textContent?.trim()))
  return { outer, named }
}

for (const card of ['control', 'my']) {
  test(`a person's ${card} Card editor shows the 49 undecided operations disabled, the 45 it decides editable`, async (t) => {
    await withBrowser(t, async (page, origin) => {
      const errors = []
      page.on('pageerror', (error) => errors.push(String(error)))
      await page.goto(`${origin}tests/fixtures/w739-person-ops.html?card=${card}`, { waitUntil: 'domcontentloaded' })
      const edit = page.getByRole('button', { name: 'Edit', exact: true })
      await edit.first().waitFor({ state: 'visible', timeout: 15000 })
      await edit.first().click()
      await page.locator('.resource-operations .outer-operation-editor').first().waitFor({ state: 'attached', timeout: 15000 })
      const { outer, named } = await page.evaluate(readRows)
      t.diagnostic(`${card}: ${outer.length} outer rows (${outer.filter((r) => r.disabled).length} disabled), ${named.length} named rows (${named.filter((r) => r.disabled).length} disabled)`)
      assert.deepEqual(errors, [])
      assert.deepEqual(outer.map((r) => r.name).sort(), [...UNDECIDED, ...DECLARED].sort(), 'all 94 outer rows are shown')
      const disabled = outer.filter((r) => r.disabled && r.managed).map((r) => r.name).sort()
      assert.deepEqual(disabled, UNDECIDED, 'exactly the 49 undecided rows are disabled, "Set by the application"')
      assert.ok(disabled.includes('project.control.get'))
      assert.deepEqual(outer.filter((r) => !r.disabled && !r.managed).map((r) => r.name).sort(), DECLARED)
      // A disabled row is ticked exactly as the Card holds it.
      assert.deepEqual(outer.filter((r) => r.disabled && r.checked).map((r) => r.name).sort(),
        ['project.control.get', 'work.item.save'])
      if (card === 'control') {
        assert.deepEqual(named.filter((r) => r.disabled && r.managed).map((r) => r.name).sort(),
          [...table.alias_role_only].sort(), 'the six role-only aliases are shown disabled on the Control Card')
        assert.equal(named.length, table.aliases.length)
        assert.deepEqual(named.filter((r) => r.disabled && r.checked).map((r) => r.name), ['object.action.project.role.get'])
      } else {
        // A My Card that declares person_card_operations shows its named aliases as the Control Card does.
        assert.deepEqual(named.filter((r) => r.disabled && r.managed).map((r) => r.name).sort(), [...table.alias_role_only].sort())
      }
    })
  })
}
