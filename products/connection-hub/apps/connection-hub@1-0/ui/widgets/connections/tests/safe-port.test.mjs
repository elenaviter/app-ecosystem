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
    if (['node_modules', 'dist', 'public', 'test-results', '_shared'].includes(name) || name.startsWith('.')) continue
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

// The modules a test file can open a listening socket through. Both quote
// styles and require() count; an alias (`createServer as make`) is still the
// imported name.
const SOCKET_MODULES = ['vite', 'node:net', 'net', 'node:http', 'http', 'node:https', 'https', 'node:http2', 'http2', 'node:dgram', 'dgram', 'ws']
const moduleImported = (text, name) => new RegExp(`(?:from\\s*|import\\s*\\(\\s*|require\\s*\\(\\s*)(['"\`])${name.replace(/[.:]/g, '\\$&')}\\1`).test(text)
const HELPER_CALL = /\b(?:inProcessViteServer|safeViteServer)\(/

/**
 * What a source file would do to the port policy (W605). A file that can open
 * a socket must take every Vite server block from the helper, may not start
 * Vite's preview server, and may not call .listen( itself; the helper file and
 * this test are the only allowed exceptions. Anywhere in the widget, a listen
 * on a literal port, a WebSocketServer, or a child process that runs Vite is
 * refused too.
 *
 * What this text scan cannot see (named, not claimed): a server started in a
 * package outside this widget, or behind an indirection that hides both the
 * module name and a literal port (a computed port handed to a re-exported
 * factory); and the Python test servers, which stay on OS-allocated loopback
 * ports with no reserved-set check.
 */
export function scanTestServerSource(path, text) {
  const offenders = []
  const vite = moduleImported(text, 'vite')
  const node = SOCKET_MODULES.filter((name) => name !== 'vite').some((name) => moduleImported(text, name))
  if (vite && /\bcreateServer\b/.test(text) && !HELPER_CALL.test(text)) offenders.push(`${path}: a Vite server without the safe-port helper`)
  if (vite && /\bpreview\b/.test(text)) offenders.push(`${path}: a Vite preview server (not covered by the safe-port helper)`)
  if ((vite || node) && /\bmiddlewareMode\s*:\s*true\b/.test(text)) offenders.push(`${path}: a literal middleware block (use inProcessViteServer())`)
  if ((vite || node) && /\bstrictPort\s*:|\bport\s*:\s*\d/.test(text)) offenders.push(`${path}: a hand-written port block (use safeViteServer())`)
  if (node && /\.listen\s*\(/.test(text)) offenders.push(`${path}: a socket listener of its own (use allocateLoopbackPort() / safeViteServer())`)
  if (/\.listen\s*\(\s*\d/.test(text)) offenders.push(`${path}: a listen on a literal port`)
  if (/\bnew\s+WebSocketServer\b/.test(text)) offenders.push(`${path}: a WebSocket server of its own`)
  if ((moduleImported(text, 'node:child_process') || moduleImported(text, 'child_process')) && /\bvite\b/.test(text)) offenders.push(`${path}: Vite run as a child process (a CLI dev server)`)
  return offenders
}

const SCAN_EXEMPT = new Set(['tests/safe-port.mjs', 'tests/safe-port.test.mjs'])

test('the bypass scan catches the bypasses it knows, and names what it cannot see', () => {
  const caught = (text) => scanTestServerSource('case.mjs', text)
  const bypasses = {
    S1: "import { createServer } from 'vite'\nawait (await createServer({ server: { port: 5173 } })).listen()",
    S2: 'import { createServer } from "vite"\nawait (await createServer({ server: { port: 5173 } })).listen()',
    S3: "import { createServer as makeServer } from 'vite'\nawait (await makeServer({})).listen()",
    S4: "import { createServer } from 'node:http'\ncreateServer(() => {}).listen(5173, '0.0.0.0')",
    S5: "import net from 'node:net'\nnet.createServer().listen(24678)",
    S6: "import { createServer } from 'node:net'\ncreateServer().listen(0, '127.0.0.1')",
    S7: "import { createServer } from 'vite'\nimport { safeViteServer } from './safe-port.mjs'\nawait createServer({ server: { middlewareMode: true } })",
    S8: "import { preview } from 'vite'\nawait preview({ preview: { port: 5173 } })",
    'S8 default port': "import { preview } from 'vite'\nawait preview({})",
    'require(http)': "const http = require('http')\nhttp.createServer().listen(8010)",
    'dynamic import': "const { createServer } = await import('node:https')\ncreateServer().listen(443)",
    'template quotes': "import { createServer } from `vite`\nawait createServer({})",
    'a re-exported factory on a literal port': "import { makeServer } from './server-factory.mjs'\nmakeServer().listen(5173)",
    'a WebSocketServer': "import { WebSocketServer } from 'ws'\nnew WebSocketServer({ port: 24678 })",
    'vite as a child process': "import { spawn } from 'node:child_process'\nspawn('npx', ['vite', '--port', '5173'])",
  }
  for (const [name, text] of Object.entries(bypasses)) assert.notDeepEqual(caught(text), [], name)
  const allowed = {
    'the helper': "import { createServer } from 'vite'\nimport { safeViteServer } from './safe-port.mjs'\nconst server = await createServer({ server: await safeViteServer() })\nawait server.listen()",
    'in-process': "import { createServer } from 'vite'\nimport { inProcessViteServer } from './safe-port.mjs'\nawait createServer({ server: inProcessViteServer() })",
    'a source-inspection test': "import { readFileSync } from 'node:fs'\nassert.match(source, /strictPort: true/)",
  }
  for (const [name, text] of Object.entries(allowed)) assert.deepEqual(caught(text), [], name)
})

test('every test server in the widget goes through the helper', () => {
  const offenders = []
  // The whole widget, not only tests/ and src/: a server helper anywhere in it
  // (tools/, scripts/) is reached by a test.
  for (const url of files(ROOT)) {
    const path = url.pathname.slice(ROOT.pathname.length)
    if (SCAN_EXEMPT.has(path)) continue
    offenders.push(...scanTestServerSource(path, readFileSync(url, 'utf8')))
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
