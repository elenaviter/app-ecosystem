// Shared by the widget's browser layout tests: a Chromium this machine already
// has, and a Vite server over the widget root that serves tests/fixtures.

import { existsSync, readdirSync } from 'node:fs'
import { createServer as createNetServer } from 'node:net'
import { homedir } from 'node:os'
import { join } from 'node:path'

import react from '@vitejs/plugin-react'
import { chromium } from 'playwright-core'
import { createServer, loadConfigFromFile } from 'vite'

// A browser this machine already has: an explicit path, Playwright's own
// install, or any cached Chromium build. None found means the caller skips and
// says why, instead of failing on a download it cannot make.
export async function launchBrowser() {
  const explicit = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
  if (explicit) return chromium.launch({ executablePath: explicit })
  try {
    return await chromium.launch()
  } catch {
    const cache = join(homedir(), 'Library', 'Caches', 'ms-playwright')
    const candidates = existsSync(cache) ? readdirSync(cache).sort().reverse().flatMap((dir) => [
      join(cache, dir, 'chrome-headless-shell-mac-arm64', 'chrome-headless-shell'),
      join(cache, dir, 'chrome-mac-arm64', 'Google Chrome for Testing.app', 'Contents', 'MacOS', 'Google Chrome for Testing'),
      join(cache, dir, 'chrome-mac', 'Chromium.app', 'Contents', 'MacOS', 'Chromium'),
      join(cache, dir, 'chrome-linux', 'chrome'),
    ]) : []
    const found = candidates.find((path) => existsSync(path))
    return found ? chromium.launch({ executablePath: found }) : null
  }
}

// A port the OS just handed out. Vite reads port 0 as "unset" and falls back
// to 5173, and on dev-main 127.0.0.1:5173 shadows the live UI's proxy port
// (2026-10-06 15:03), so the fixture never lets Vite choose.
async function freePort() {
  return new Promise((resolve, reject) => {
    const probe = createNetServer()
    probe.unref()
    probe.on('error', reject)
    probe.listen(0, '127.0.0.1', () => {
      const { port } = probe.address()
      probe.close(() => resolve(port))
    })
  })
}

export async function startFixtureServer() {
  const port = await freePort()
  if (port === 5173) throw new Error('fixture refused port 5173 (the live UI proxy port)')
  const root = new URL('..', import.meta.url).pathname
  const loaded = await loadConfigFromFile({ command: 'serve', mode: 'test' }, join(root, 'vite.config.ts'))
  const server = await createServer({
    root,
    configFile: false,
    plugins: [react()],
    resolve: { alias: loaded.config.resolve.alias },
    server: { port, host: '127.0.0.1', strictPort: true },
    logLevel: 'silent',
  })
  await server.listen()
  return server
}
