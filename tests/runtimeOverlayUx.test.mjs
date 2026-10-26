import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const appSource = readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8')
const mainSource = readFileSync(new URL('../src/main.ts', import.meta.url), 'utf8')
const manifest = JSON.parse(readFileSync(new URL('../python/runtime-manifest.json', import.meta.url), 'utf8'))

test('an older runtime only prompts for confirmation and model preloading waits for dependency synchronization', () => {
  assert.doesNotMatch(appSource, /app\.updateRuntimeCore\(/)
  assert.doesNotMatch(appSource, /synchronizeRuntimeManifest|runtimeAutoSyncActive/)
  assert.match(appSource, /runtimeCorePromptVisible\.value = true/)
  assert.match(mainSource, /!runtimeCoreSyncAvailable\(activeRuntime, runtime\.manifestVersion\)/)
})

test('runtime overlay releases declare a stable base generation and exact core versions', () => {
  assert.equal(manifest.baseGeneration, 1)
  assert.match(manifest.common.pymss, /==\d+\.\d+\.\d+$/)
  assert.match(manifest.common['pymss-core'], /==\d+\.\d+\.\d+$/)
})
