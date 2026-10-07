import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { createServer as createNetServer } from 'node:net'
import test from 'node:test'

import { createServer } from 'vite'

import { startFixtureServer } from './browser-fixture.mjs'
import {
  LOOPBACK, RESERVED_PORTS, allocateLoopbackPort, assertSafeViteServer, inProcessViteServer, isReservedPort, safeViteServer,
} from './safe-port.mjs'

// W605: no test server in this widget can take a live service port.

const ROOT = new URL('../', import.meta.url)

function files(dir, out = []) {
  for (const name of readdirSync(dir)) {
    if (name === 'node_modules' || name === 'dist') continue
    const url = new URL(name, dir)
    if (statSync(url).isDirectory()) files(new URL(`${name}/`, dir), out)
    else if (/\.(mjs|js|ts|tsx)$/.test(name)) out.push(url)
  }
  return out
}

// The sockets this process is listening on, from the kernel's own table.
function listening() {
  try {
    return execFileSync('ss', ['-ltnpH'], { encoding: 'utf8' })
      .split('\n').filter((line) => line.includes(`pid=${process.pid},`))
      .map((line) => line.trim().split(/\s+/)[3])
  } catch {
    return null
  }
}

// The same policy file lives in the Problem Board widget (Applications) and the
// Connection Hub widget (App Ecosystem). A change to one copy fails here until
// both copies and both pins move together.
const SAFE_PORT_SHA256 = 'da48b62f39ffdbdc665fd6829aaabe9271efc67b63bbfed0549dd86316e512ef'

test('this copy of safe-port.mjs is the shared policy, byte for byte', () => {
  const digest = createHash('sha256').update(readFileSync(new URL('./safe-port.mjs', import.meta.url))).digest('hex')
  assert.equal(digest, SAFE_PORT_SHA256, "update both widgets' safe-port.mjs and both pins together")
})

test('the reserved list covers every live port the source-test procedure names, and the HMR port', () => {
  for (const port of [443, 5173, 5432, 6379, 7781, 8010, 18080, 24678]) assert.ok(isReservedPort(port), String(port))
  assert.equal(new Set(RESERVED_PORTS).size, RESERVED_PORTS.length)
})

test('an unsafe Vite server block is refused before anything listens', () => {
  const ok = { port: 40123, host: LOOPBACK, strictPort: true }
  assert.equal(assertSafeViteServer(ok), ok)
  const refused = {
    'literal port zero (Vite falls back to 5173)': { ...ok, port: 0 },
    'no port at all': { host: LOOPBACK, strictPort: true },
    'a live port': { ...ok, port: 5173 },
    'the HMR port': { ...ok, port: 24678 },
    'non-strict fallback': { ...ok, strictPort: false },
    'strictPort missing': { port: 40123, host: LOOPBACK },
    'every interface': { ...ok, host: '0.0.0.0' },
    'middleware with HMR on': { middlewareMode: true },
    'middleware with the websocket on': { middlewareMode: true, hmr: false },
  }
  for (const [name, server] of Object.entries(refused)) assert.throws(() => assertSafeViteServer(server), Error, name)
  assert.deepEqual(inProcessViteServer(), { middlewareMode: true, hmr: false, ws: false })
})

test('allocation skips a reserved or invalid answer a bounded number of times, then refuses', async () => {
  const answers = [5173, 0, 24678, 41000]
  assert.equal(await allocateLoopbackPort({ allocate: async () => answers.shift() }), 41000)
  let calls = 0
  await assert.rejects(
    allocateLoopbackPort({ attempts: 3, allocate: async () => { calls += 1; return 5173 } }),
    /No safe loopback test port after 3 attempts; refusing to fall back/,
  )
  assert.equal(calls, 3, 'bounded: no endless retry')
  const real = await allocateLoopbackPort()
  assert.ok(real > 0 && !isReservedPort(real))
})

test('every test server in the widget goes through the helper', () => {
  const offenders = []
  for (const url of [...files(new URL('tests/', ROOT)), ...files(new URL('src/', ROOT))]) {
    const path = url.pathname.slice(ROOT.pathname.length)
    if (path === 'tests/safe-port.mjs' || path === 'tests/safe-port.test.mjs') continue
    const text = readFileSync(url, 'utf8')
    const vite = /from 'vite'/.test(text) && /\bcreateServer\(/.test(text)
    if (vite && !/\b(?:inProcessViteServer|safeViteServer)\(/.test(text)) offenders.push(`${path}: a Vite server without the safe-port helper`)
    if (/middlewareMode:\s*true/.test(text)) offenders.push(`${path}: a literal middleware block (use inProcessViteServer())`)
    // Only a file that can open a socket (it imports Vite or node:net) is held
    // to these; a test that inspects another file's source may quote them.
    const opens = /from 'vite'|from 'node:net'/.test(text)
    if (opens && /\bstrictPort:|\bport:\s*0\b/.test(text)) offenders.push(`${path}: a hand-written port block (use safeViteServer())`)
    if (opens && /\.listen\(\s*0\b/.test(text)) offenders.push(`${path}: its own port probe (use allocateLoopbackPort())`)
  }
  assert.deepEqual(offenders, [])
})

test('a held port is refused, not swapped for another one', async () => {
  const holder = createNetServer()
  await new Promise((resolve) => holder.listen(0, LOOPBACK, resolve))
  const held = holder.address().port
  try {
    const vite = await createServer({ configFile: false, root: new URL('.', ROOT).pathname, logLevel: 'silent',
      server: assertSafeViteServer({ port: held, host: LOOPBACK, strictPort: true }) })
    try {
      await assert.rejects(vite.listen(), /already in use|EADDRINUSE/i)
    } finally {
      await vite.close()
    }
  } finally {
    await new Promise((resolve) => holder.close(resolve))
  }
})

test('a disposable run: the fixture listens on one positive loopback port, an in-process server on none', async (t) => {
  const before = listening()
  if (before === null) t.diagnostic('ss is unavailable; the socket table is not checked')
  const server = await startFixtureServer()
  const address = server.httpServer.address()
  try {
    assert.equal(address.address, LOOPBACK)
    assert.ok(address.port > 0 && !isReservedPort(address.port))
    assert.equal(server.config.server.strictPort, true)
    t.diagnostic(`fixture port ${address.port}`)
    if (before !== null) assert.ok(listening().includes(`${LOOPBACK}:${address.port}`))
  } finally {
    await server.close()
  }
  const inProcess = await createServer({ configFile: false, root: new URL('.', ROOT).pathname, logLevel: 'silent',
    appType: 'custom', server: inProcessViteServer() })
  try {
    if (before !== null) {
      const now = listening()
      assert.deepEqual(now.filter((socket) => !before.includes(socket)), [], 'the in-process server opened no socket')
      assert.ok(!now.some((socket) => socket.endsWith(':24678')), 'no HMR websocket on 24678')
    }
  } finally {
    await inProcess.close()
  }
  if (before !== null) assert.ok(!listening().includes(`${LOOPBACK}:${address.port}`), 'the fixture port was released')
  const config = await safeViteServer()
  assert.ok(config.port > 0 && config.strictPort === true && config.host === LOOPBACK)
})
