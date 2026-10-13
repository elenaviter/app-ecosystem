// W775 (operator, 2026-10-13: "(b) on a person's Card in Connection Hub, fold the agent-only tools under
// \"Agent-only tools (not for people)\" without the amber INACTIVE warning. << b) - yes."). Mounted: the real
// DelegatedAccessPanel in Chromium (tests/fixtures/w775-agent-only.tsx). A person's My Card under an active
// Control that leaves every agent-only operation out shows those operations folded, collapsed, with no
// "inactive" badge or "must also allow" note; an agent's Card under the same Control is unchanged.
import assert from 'node:assert/strict'
import test from 'node:test'

import { launchBrowser, startFixtureServer } from './browser-fixture.mjs'

const AGENT = ['assignment.report', 'attachment.request_upload', 'control.acknowledge', 'control.discard_complete',
  'control.pull', 'control.refuse', 'control.worker_settle', 'event.publish', 'journal.view.fail', 'journal.view.publish',
  'mail.reconciliation.publish', 'mail.route', 'note.view.fail', 'note.view.publish', 'operation.receipt.get',
  'plan.nodes.publish', 'project.coordinator.note.write', 'project.file.edit.result', 'project.github.use',
  'project.role.handover.decide', 'session.resume.fail', 'session.resume.publish', 'worker.heartbeat', 'worker.publish',
  'workspace.shared_write.clear', 'workspace.shared_write.publish'].sort()

async function withBrowser(t, run) {
  const browser = await launchBrowser()
  if (!browser) {
    t.skip('no Chromium available; set PLAYWRIGHT_CHROMIUM_EXECUTABLE to run the mounted W775 check')
    return
  }
  const server = await startFixtureServer()
  const origin = server.resolvedUrls.local[0]
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  await page.route('**/*', (route) => route.request().url().endsWith('/csrf')
    ? route.fulfill({ status: 200, contentType: 'application/json', body: '{"csrf_required":false}' })
    : route.request().url().startsWith(origin) ? route.continue() : route.abort())
  try {
    await page.goto(`${origin}tests/fixtures/w775-agent-only.html`, { waitUntil: 'load' })
    await page.waitForTimeout(3000)
    await run(page, origin)
  } finally {
    await page.close()
    await server.close()
    await browser.close()
  }
}

/** Every outer row of the editor: its id, whether it is inside the fold, and its Control warnings. */
function readRows() {
  return [...document.querySelectorAll('.resource-operations .outer-operation-editor')].map((element) => ({
    name: (element.querySelector('code.operation-id') || element.querySelector('.operation-name span'))?.textContent?.trim(),
    folded: Boolean(element.closest('details.agent-only-operations')),
    inactive: [...element.querySelectorAll('.badge-warn')].some((badge) => /inactive/i.test(badge.textContent || '')),
    note: Boolean(element.querySelector('.control-cap-note')),
  }))
}

async function openEditor(page, origin, card) {
  const errors = []
  page.on('pageerror', (error) => errors.push(String(error)))
  await page.goto(`${origin}tests/fixtures/w775-agent-only.html?card=${card}`, { waitUntil: 'domcontentloaded' })
  const edit = page.getByRole('button', { name: 'Edit', exact: true })
  await edit.first().waitFor({ state: 'visible', timeout: 15000 })
  await edit.first().click()
  await page.locator('.resource-operations .outer-operation-editor').first().waitFor({ state: 'attached', timeout: 15000 })
  return errors
}

test("a person's My Card folds the agent-only operations, collapsed, with no Control warning", async (t) => {
  await withBrowser(t, async (page, origin) => {
    const errors = await openEditor(page, origin, 'my')
    const rows = await page.evaluate(readRows)
    const fold = await page.evaluate(() => {
      const details = document.querySelector('details.agent-only-operations')
      return details ? { open: details.open, summary: details.querySelector('summary')?.textContent?.trim() } : null
    })
    t.diagnostic(`my: ${rows.length} rows, ${rows.filter((r) => r.folded).length} folded`)
    assert.deepEqual(errors, [])
    assert.deepEqual(fold, { open: false, summary: 'Agent-only tools (not for people)' })
    assert.deepEqual(rows.filter((r) => r.folded).map((r) => r.name).sort(), AGENT)
    assert.deepEqual(rows.filter((r) => r.folded && (r.inactive || r.note)).map((r) => r.name), [],
      'no folded row carries the amber inactive badge or the "must also allow" note')
    assert.ok(rows.some((r) => !r.folded && r.name === 'plan.item.update'), 'person-decided rows stay outside the fold')
    // The Card-level notice of inactive selections never lists a held agent-only operation (worker.publish, mail.route).
    await page.locator('.control-cap-warning button, .control-cap-warning summary').first().click().catch(() => {})
    const warning = await page.evaluate(() => document.querySelector('.control-cap-warning')?.textContent || '')
    t.diagnostic(`card-level warning: ${warning.slice(0, 200)}`)
    assert.ok(!/worker\.publish|mail\.route/.test(warning), 'held agent-only operations are not counted as inactive selections')
  })
})

test("an agent's Card under the same Control is unchanged: no fold, its own operations warned as before", async (t) => {
  await withBrowser(t, async (page, origin) => {
    const errors = await openEditor(page, origin, 'agent')
    const rows = await page.evaluate(readRows)
    t.diagnostic(`agent: ${rows.length} rows, ${rows.filter((r) => r.inactive).length} inactive`)
    assert.deepEqual(errors, [])
    assert.equal(await page.locator('details.agent-only-operations').count(), 0)
    assert.equal(rows.some((r) => r.folded), false)
    const workerPublish = rows.find((r) => r.name === 'worker.publish')
    assert.ok(workerPublish && workerPublish.inactive, 'outside its Control, worker.publish is still shown inactive on an agent Card')
  })
})
