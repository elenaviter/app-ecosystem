// Shared by the widget's browser layout tests: a Chromium this machine already
// has, and a Vite server over the widget root that serves tests/fixtures.

import { existsSync, readdirSync } from 'node:fs'
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

export async function startFixtureServer() {
  const root = new URL('..', import.meta.url).pathname
  const loaded = await loadConfigFromFile({ command: 'serve', mode: 'test' }, join(root, 'vite.config.ts'))
  const server = await createServer({
    root,
    configFile: false,
    plugins: [react()],
    resolve: { alias: loaded.config.resolve.alias },
    server: { port: 0, host: '127.0.0.1', strictPort: false },
    logLevel: 'silent',
  })
  await server.listen()
  return server
}
