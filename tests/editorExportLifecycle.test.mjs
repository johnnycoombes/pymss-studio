import assert from 'node:assert/strict'
import test, { after, afterEach } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createPinia } from 'pinia'

const vite = await createServer({
  configFile: false,
  server: { watch: null, middlewareMode: true, hmr: false, preTransformRequests: false },
  appType: 'custom',
  optimizeDeps: { noDiscovery: true },
  resolve: { alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) } },
})
after(() => vite.close())

globalThis.window = {
  matchMedia: () => ({ matches: false }),
  addEventListener() {},
  removeEventListener() {},
}
Object.defineProperty(globalThis, 'navigator', {
  value: { language: 'en' },
  configurable: true,
})
const { useEditorStore, EditorExportCancelledError } = await vite.ssrLoadModule('/src/stores/editor.ts')
const stores = []
const flush = async () => { await new Promise(resolve => setImmediate(resolve)) }

function deferred() {
  let resolve
  let reject
  const promise = new Promise((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

function environment({ startGate, cancelAccepted = true, nativeRenderGate } = {}) {
  const callbacks = new Map()
  const listeners = new Map()
  const calls = []
  let callbackId = 0
  globalThis.window = {
    __TAURI_INTERNALS__: {
      transformCallback(handler) { callbacks.set(++callbackId, handler); return callbackId },
      async invoke(command, args) {
        if (command === 'plugin:event|listen') {
          listeners.set(args.handler, { event: args.event, handler: callbacks.get(args.handler) })
          return args.handler
        }
        if (command === 'plugin:event|unlisten') {
          listeners.delete(args.eventId)
          return null
        }
        calls.push({ command, args })
        if (command === 'plugin_host_capabilities') return { enabled: true, experimental: true }
        if (command === 'save_editor_project') return args.project
        if (command === 'plugin_host_render') {
          if (nativeRenderGate) return nativeRenderGate.promise
          return { path: 'D:/native-render.wav', duration: 1.5, sampleRate: 48000, channels: 2, tailLimited: true }
        }
        if (command === 'plugin_host_cancel_render') { nativeRenderGate?.reject(new Error('Rendering cancelled')); return true }
        if (command === 'plugin_host_cleanup_render') return null
        if (command === 'start_editor_mix_export') {
          if (startGate) await startGate.promise
          return { taskId: args.payload.taskId, started: true }
        }
        if (command === 'cancel_editor_mix_export') {
          if (cancelAccepted) {
            dispatch('pymss://worker-event', {
              type: 'task_cancelled', taskId: args.taskId, payload: { message: 'Cancelled' },
            })
          }
          return cancelAccepted
        }
        throw new Error(`Unexpected IPC command: ${command}`)
      },
    },
    __TAURI_EVENT_PLUGIN_INTERNALS__: { unregisterListener() {} },
  }

  function dispatch(event, payload) {
    for (const listener of listeners.values()) {
      if (listener.event === event) listener.handler({ event, payload })
    }
  }

  const store = useEditorStore(createPinia())
  stores.push(store)
  store.session = {
    id: 'project',
    name: 'Project',
    masterVolume: 1,
    masterPan: 0,
    sources: [{
      id: 'source', role: 'stem', path: 'D:/audio.wav', name: 'audio.wav',
      duration: 1, sampleRate: 48000, channels: 2, missing: false,
    }],
    tracks: [{
      id: 'track', sourceId: 'source', role: 'stem', name: 'Track', volume: 1,
      pan: 0, muted: false, solo: false, fadeIn: 0, fadeOut: 0,
    }],
    createdAt: 1,
    updatedAt: 1,
  }
  return { store, calls, dispatch }
}

afterEach(() => {
  for (const store of stores.splice(0)) store.$dispose()
  Reflect.deleteProperty(globalThis, 'window')
})

test('background editor export publishes progress and resolves its terminal result', async () => {
  const env = environment()
  const exporting = env.store.exportMix({ format: 'wav' })
  await flush()
  const start = env.calls.find(call => call.command === 'start_editor_mix_export')
  assert.ok(start)
  const taskId = start.args.payload.taskId

  env.dispatch('pymss://worker-event', {
    type: 'editor_export_progress', taskId,
    payload: { phase: 'rendering', completed: 54, total: 100, current: 'audio.wav' },
  })
  assert.equal(env.store.exportProgress.completed, 54)
  assert.equal(env.store.exportProgress.current, 'audio.wav')
  env.dispatch('pymss://worker-event', {
    type: 'editor_mix_exported', taskId,
    payload: { path: 'D:/mix.wav', duration: 1, sampleRate: 48000, channels: 2, format: 'wav' },
  })

  const result = await exporting
  assert.equal(result.path, 'D:/mix.wav')
  assert.equal(env.store.exporting, false)
  assert.equal(env.store.lastExport.path, 'D:/mix.wav')
})

test('cancellation requested before the start ACK waits before terminating the worker', async () => {
  const startGate = deferred()
  const env = environment({ startGate })
  const exporting = env.store.exportMix({ format: 'wav' })
  await flush()
  const cancelling = env.store.cancelExport()
  await flush()
  assert.equal(env.calls.filter(call => call.command === 'cancel_editor_mix_export').length, 0)

  startGate.resolve()
  assert.equal(await cancelling, true)
  await assert.rejects(exporting, EditorExportCancelledError)
  assert.equal(env.calls.filter(call => call.command === 'cancel_editor_mix_export').length, 1)
  assert.equal(env.store.exporting, false)
})

test('cancellation during listener initialization prevents the worker from starting', async () => {
  const env = environment()
  const exporting = env.store.exportMix({ format: 'wav' })
  assert.equal(await env.store.cancelExport(), true)

  await assert.rejects(exporting, EditorExportCancelledError)
  assert.equal(env.calls.filter(call => call.command === 'start_editor_mix_export').length, 0)
  assert.equal(env.store.exporting, false)
})

test('a late cancellation cannot replace an already accepted completion event', async () => {
  const env = environment({ cancelAccepted: false })
  const exporting = env.store.exportMix({ format: 'wav' })
  await flush()
  const start = env.calls.find(call => call.command === 'start_editor_mix_export')
  const taskId = start.args.payload.taskId

  assert.equal(await env.store.cancelExport(), false)
  env.dispatch('pymss://worker-event', {
    type: 'editor_mix_exported', taskId,
    payload: { path: 'D:/mix.wav', duration: 1, sampleRate: 48000, channels: 2, format: 'wav' },
  })

  const result = await exporting
  assert.equal(result.path, 'D:/mix.wav')
  assert.equal(env.store.lastError, null)
})

test('a non-plugin export sends the project snapshot directly to the encoder without render chunks', async () => {
  const env = environment()
  env.store.session.masterVolume = 0.8
  env.store.session.tracks[0].volume = 0.6
  const exporting = env.store.exportMix({ format: 'wav' })
  await flush()
  const start = env.calls.find(call => call.command === 'start_editor_mix_export')
  assert.ok(start)
  assert.equal(start.args.payload.project.masterVolume, 0.8)
  assert.equal(start.args.payload.project.tracks[0].volume, 0.6)
  assert.equal(start.args.payload.project.assets[0].path, 'D:/audio.wav')
  assert.deepEqual(env.calls.map(call => call.command).filter(command => command !== 'plugin_host_capabilities'), ['start_editor_mix_export'])
  env.dispatch('pymss://worker-event', { type: 'editor_mix_exported', taskId: start.args.payload.taskId,
    payload: { path: 'D:/mix.wav', format: 'wav', duration: 1, sampleRate: 48000, channels: 2 } })
  assert.equal((await exporting).path, 'D:/mix.wav')
})

const pluginSlot = { id: 'slot', format: 'vst3', pluginId: 'gain', name: 'Gain', path: 'gain.vst3', bypassed: false, state: 'sha256:opaque' }

test('exports with all master and track plugins disabled skip native rendering and preserve saved slots', async () => {
  const env = environment()
  const disabled = { ...pluginSlot, bypassed: true }
  env.store.session.pluginChain = [disabled]
  env.store.session.tracks[0].pluginChain = [{ ...disabled, id: 'track-fx' }]
  const before = JSON.parse(JSON.stringify(env.store.session))
  const exporting = env.store.exportMix()
  await flush()
  assert.equal(env.calls.some(call => call.command === 'plugin_host_render'), false)
  const start = env.calls.find(call => call.command === 'start_editor_mix_export')
  assert.equal(start.args.payload.project.assets[0].path, 'D:/audio.wav')
  env.dispatch('pymss://worker-event', { type: 'editor_mix_exported', taskId: start.args.payload.taskId,
    payload: { path: 'D:/dry.wav', duration: 1, sampleRate: 48000, channels: 2, format: 'wav' } })
  await exporting
  assert.deepEqual(env.store.session, before)
  assert.equal(env.calls.some(call => call.command === 'plugin_host_cleanup_render'), false)
})

test('an enabled track plugin still requires native export beside a disabled master plugin', async () => {
  const env = environment()
  env.store.session.pluginChain = [{ ...pluginSlot, bypassed: true }]
  env.store.session.tracks[0].pluginChain = [{ ...pluginSlot, id: 'track-fx' }]
  const exporting = env.store.exportMix()
  await flush()
  assert.equal(env.calls.some(call => call.command === 'plugin_host_render'), true)
  const start = env.calls.find(call => call.command === 'start_editor_mix_export')
  env.dispatch('pymss://worker-event', { type: 'editor_mix_exported', taskId: start.args.payload.taskId,
    payload: { path: 'D:/wet.wav', duration: 1.5, sampleRate: 48000, channels: 2, format: 'wav' } })
  await exporting
})

test('the enabled master limiter still renders when every plugin is disabled', async () => {
  const env = environment()
  env.store.session.pluginChain = [{ ...pluginSlot, bypassed: true }]
  env.store.setMasterLimiter({ enabled: true, gainDb: 6 })
  const exporting = env.store.exportMix()
  await flush()
  const render = env.calls.find(call => call.command === 'plugin_host_render')
  assert.equal(render.args.project.masterLimiter.enabled, true)
  const start = env.calls.find(call => call.command === 'start_editor_mix_export')
  env.dispatch('pymss://worker-event', { type: 'editor_mix_exported', taskId: start.args.payload.taskId,
    payload: { path: 'D:/limited.wav', duration: 1, sampleRate: 48000, channels: 2, format: 'wav' } })
  await exporting
  assert.equal(env.store.session.pluginChain[0].bypassed, true)
})

test('plugin rendering is encoded once with neutral master and cleaned after completion', async () => {
  const env = environment()
  env.store.session.masterVolume = 0.5
  env.store.session.pluginChain = [{ ...pluginSlot }]
  const exporting = env.store.exportMix({ format: 'flac' })
  await flush()
  const render = env.calls.find(call => call.command === 'plugin_host_render')
  const start = env.calls.find(call => call.command === 'start_editor_mix_export')
  assert.equal(render.args.project.masterVolume, 0.5)
  assert.equal(render.args.project.pluginChain[0].state, 'sha256:opaque')
  assert.equal(start.args.payload.project.masterVolume, 1)
  assert.equal(start.args.payload.project.masterPan, 0)
  assert.equal(start.args.payload.project.tracks.length, 1)
  assert.equal(start.args.payload.project.tracks[0].volume, 1)
  assert.equal(start.args.payload.project.assets[0].path, 'D:/native-render.wav')
  env.dispatch('pymss://worker-event', { type: 'editor_mix_exported', taskId: start.args.payload.taskId,
    payload: { path: 'D:/mix.flac', format: 'flac', duration: 1.5, sampleRate: 48000, channels: 2 } })
  const result = await exporting
  assert.equal(result.pluginTailLimited, true)
  assert.equal(env.store.lastExport.pluginTailLimited, true)
  assert.equal(env.calls.filter(call => call.command === 'plugin_host_cleanup_render').length, 1)
})

test('cancelling native rendering terminates the render without starting the encoder', async () => {
  const nativeRenderGate = deferred()
  const env = environment({ nativeRenderGate })
  env.store.session.pluginChain = [{ ...pluginSlot }]
  const exporting = env.store.exportMix()
  await flush()
  assert.ok(env.calls.some(call => call.command === 'plugin_host_render'))
  await env.store.cancelExport()
  await assert.rejects(exporting, EditorExportCancelledError)
  assert.equal(env.calls.filter(call => call.command === 'plugin_host_cancel_render').length, 1)
  assert.equal(env.calls.filter(call => call.command === 'start_editor_mix_export').length, 0)
  assert.equal(env.calls.filter(call => call.command === 'plugin_host_cleanup_render').length, 1)
})

test('history and persistence retain independent master and track chains with unknown states', async () => {
  const env = environment()
  const unknown = { ...pluginSlot, format: 'au', state: 'opaque:keep', extension: { revision: 9 } }
  env.store.setPluginChain(null, [unknown])
  env.store.setPluginChain('track', [{ ...pluginSlot, id: 'track-slot' }])
  env.store.undo()
  assert.equal(env.store.session.tracks[0].pluginChain, undefined)
  assert.equal(env.store.session.pluginChain[0].state, 'opaque:keep')
  env.store.redo()
  assert.equal(env.store.session.tracks[0].pluginChain[0].id, 'track-slot')
  await env.store.flushSave()
  const saved = env.calls.filter(call => call.command === 'save_editor_project').at(-1).args.project
  assert.equal(saved.version, 4)
  assert.deepEqual(saved.pluginChain[0], unknown)
  assert.equal(saved.tracks[0].pluginChain[0].state, 'sha256:opaque')
})

test('enabled master limiter alone renders natively at the encoding rate and clears processing before encoding', async () => {
  const env = environment()
  env.store.setMasterLimiter({ enabled: true, gainDb: 12, ceilingDb: -6 })
  const exporting = env.store.exportMix({ format: 'wav', audioParams: { sampleRate: 32000 } })
  await flush()
  const native = env.calls.find(call => call.command === 'plugin_host_render')
  assert.equal(native.args.project.masterLimiter.enabled, true)
  assert.equal(native.args.sampleRate, 32000)
  const start = env.calls.find(call => call.command === 'start_editor_mix_export')
  assert.equal(start.args.payload.project.masterLimiter, undefined)
  assert.equal(start.args.payload.project.masterVolume, 1)
  assert.equal(start.args.payload.project.masterPan, 0)
  assert.equal(start.args.payload.project.tracks.length, 1)
  assert.equal(start.args.payload.project.assets[0].path, 'D:/native-render.wav')
  env.dispatch('pymss://worker-event', { type: 'editor_mix_exported', taskId: start.args.payload.taskId,
    payload: { path: 'D:/limited.wav', duration: 1.5, sampleRate: 32000, channels: 2, format: 'wav' } })
  await exporting
})

test('disabled master limiter keeps normal exports on the existing dry path', async () => {
  const env = environment()
  env.store.setMasterLimiter({ enabled: false, gainDb: 30, ceilingDb: -30 })
  const exporting = env.store.exportMix({ format: 'wav' })
  await flush()
  assert.equal(env.calls.some(call => call.command === 'plugin_host_render'), false)
  const start = env.calls.find(call => call.command === 'start_editor_mix_export')
  assert.equal(start.args.payload.project.assets[0].path, 'D:/audio.wav')
  env.dispatch('pymss://worker-event', { type: 'editor_mix_exported', taskId: start.args.payload.taskId,
    payload: { path: 'D:/dry.wav', duration: 1, sampleRate: 48000, channels: 2, format: 'wav' } })
  await exporting
})

test('plugin rendering publishes real weighted progress before the encoder starts and never moves backwards', async () => {
  const gate = deferred()
  const env = environment({ nativeRenderGate: gate })
  env.store.session.pluginChain = [{ ...pluginSlot }]
  const exporting = env.store.exportMix()
  await flush()
  const native = env.calls.find(call => call.command === 'plugin_host_render')
  const update = (completed, total = 100, taskId = native.args.taskId, phase = 'rendering') => env.dispatch('pymss://worker-event', {
    type: 'editor_plugin_render_progress', taskId, payload: { completed, total, phase },
  })
  assert.equal(env.calls.some(call => call.command === 'start_editor_mix_export'), false)
  update(20)
  assert.equal(env.store.exportProgress.completed, 15)
  assert.equal(env.store.exportProgress.phase, 'rendering')
  update(60)
  assert.equal(env.store.exportProgress.completed, 45)
  update(40)
  update(99, 100, 'old-render')
  update(Number.NaN)
  update(200)
  assert.equal(env.store.exportProgress.completed, 45)
  gate.resolve({ path: 'D:/render.wav', duration: 1, sampleRate: 48000, channels: 2 })
  await flush()
  const start = env.calls.find(call => call.command === 'start_editor_mix_export')
  assert.equal(env.store.exportProgress.completed, 75)
  update(0)
  assert.equal(env.store.exportProgress.completed, 75, 'late native progress must not affect encoding')
  env.dispatch('pymss://worker-event', { type: 'editor_export_progress', taskId: start.args.payload.taskId,
    payload: { phase: 'writing', completed: 40, total: 100 } })
  assert.equal(env.store.exportProgress.completed, 85)
  for (const payload of [
    { phase: 'preparing', completed: 10, total: 100 },
    { phase: 'writing', completed: Number.NaN, total: 100 },
    { phase: 'writing', completed: 101, total: 100 },
    { phase: 'writing', completed: 1, total: 0 },
  ]) {
    env.dispatch('pymss://worker-event', { type: 'editor_export_progress', taskId: start.args.payload.taskId, payload })
    assert.equal(env.store.exportProgress.completed, 85)
  }
  env.dispatch('pymss://worker-event', { type: 'editor_mix_exported', taskId: start.args.payload.taskId,
    payload: { path: 'D:/mix.wav', duration: 1, sampleRate: 48000, channels: 2, format: 'wav' } })
  await exporting
})

test('direct encoding rejects invalid or regressive progress without changing its denominator', async () => {
  const env = environment()
  const exporting = env.store.exportMix()
  await flush()
  const start = env.calls.find(call => call.command === 'start_editor_mix_export')
  const taskId = start.args.payload.taskId
  const update = (completed, total) => env.dispatch('pymss://worker-event', {
    type: 'editor_export_progress', taskId, payload: { phase: 'writing', completed, total },
  })
  update(1, 2)
  assert.equal(env.store.exportProgress.completed / env.store.exportProgress.total, 0.5)
  for (const [completed, total] of [[40, 100], [NaN, 100], [1, Infinity], [-1, 100], [101, 100], [0, 0]]) {
    update(completed, total)
    assert.equal(env.store.exportProgress.completed, 1)
    assert.equal(env.store.exportProgress.total, 2)
  }
  update(100, 100)
  assert.equal(env.store.exportProgress.completed, 100)
  env.dispatch('pymss://worker-event', { type: 'editor_mix_exported', taskId,
    payload: { path: 'D:/mix.wav', duration: 1, sampleRate: 48000, channels: 2, format: 'wav' } })
  await exporting
})
