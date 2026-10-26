import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

// Exercise environment setup without starting Cargo or the desktop application.
const source = readFileSync(new URL('../scripts/tauri.mjs', import.meta.url), 'utf8').split('await import(')[0]

function environment({ args = ['dev'], tty = true, env = {} } = {}) {
  const process = { argv: ['node', 'scripts/tauri.mjs', ...args], stderr: { isTTY: tty }, env: { ...env } }
  vm.runInNewContext(source, { process })
  return process.env
}

test('interactive dev forwards color support through Tauri and Cargo', () => {
  assert.equal(environment().PYMSS_STUDIO_TERMINAL_COLOR, '1')
  assert.equal(environment().FORCE_COLOR, undefined)
  assert.equal(environment({ args: ['-v', 'dev', '--config', 'preview.json'] }).PYMSS_STUDIO_TERMINAL_COLOR, '1')
})

test('redirected dev output and non-dev commands do not force ANSI', () => {
  assert.equal(environment({ tty: false }).PYMSS_STUDIO_TERMINAL_COLOR, undefined)
  for (const args of [[], ['build'], ['--help'], ['info']]) {
    assert.equal(environment({ args }).PYMSS_STUDIO_TERMINAL_COLOR, undefined)
  }
})

test('explicit color choices and NO_COLOR are preserved', () => {
  for (const value of ['', '0', '1', '2', '3']) {
    assert.deepEqual(environment({ env: { FORCE_COLOR: value } }), { FORCE_COLOR: value })
  }
  for (const value of ['', '1']) {
    assert.deepEqual(environment({ env: { NO_COLOR: value } }), { NO_COLOR: value })
  }
  assert.deepEqual(environment({ env: { NO_COLOR: '1', FORCE_COLOR: '1' } }), { NO_COLOR: '1', FORCE_COLOR: '1' })
})
