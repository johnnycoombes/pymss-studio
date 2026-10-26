import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import { AsyncLocalStorage } from 'node:async_hooks'
import { fileURLToPath } from 'node:url'
import { createPinia } from 'pinia'
import { createServer } from 'vite'
import { Window } from 'happy-dom'

const browser = new Window()
const windows = new AsyncLocalStorage()
Object.defineProperty(globalThis, 'window', { configurable: true, get: () => windows.getStore()?.window || browser })
globalThis.document = browser.document
Object.defineProperty(globalThis, 'navigator', { value: { language: 'en' }, configurable: true })
const vite = await createServer({
  configFile: false,
  server: { watch: null, middlewareMode: true, hmr: false, preTransformRequests: false },
  appType: 'custom', optimizeDeps: { noDiscovery: true },
  resolve: { alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) } },
})
after(() => vite.close())
const { useSettingsStore } = await vite.ssrLoadModule('/src/stores/settings.ts')
const { useEditorExport } = await vite.ssrLoadModule('/src/composables/useEditorExport.ts')
const { EditorExportCancelledError } = await vite.ssrLoadModule('/src/stores/editor.ts')
const { listenForEditorExportPreferences, requestEditorExportPreference } = await vite.ssrLoadModule('/src/utils/editorExportPreferences.ts')
const clone = value => JSON.parse(JSON.stringify(value))

async function environment(t) {
  const state = { themeMode: 'light', locale: 'en', startupOnboardingSeen: true,
    outputDir: 'D:/outputs', proxyUrl: 'http://127.0.0.1:7001', wavBitDepth: 'PCM_16', flacBitDepth: 'PCM_16' }
  const callbacks = new Map()
  const listeners = new Map()
  const writes = []
  const messages = []
  const instances = []
  let sequence = 0
  let failWrites = false
  let failSend = false
  let saveGate
  async function instance(label) {
    const context = { label, window: {
      matchMedia: () => ({ matches: false, addEventListener() {} }),
      addEventListener() {}, removeEventListener() {},
      __TAURI_EVENT_PLUGIN_INTERNALS__: { unregisterListener() {} },
      __TAURI_INTERNALS__: {
        metadata: { currentWindow: { label }, currentWebview: { label } },
        transformCallback(handler) { callbacks.set(++sequence, handler); return sequence },
        async invoke(command, args) {
          if (command === 'load_app_store') return clone(state)
          if (command === 'save_app_store') {
            if (saveGate) await saveGate
            if (failWrites) throw new Error('store write failed')
            Object.assign(state, clone(args.data))
            writes.push({ label, data: clone(args.data) })
            return null
          }
          if (command === 'get_app_paths') return { modelsDir: 'D:/models', outputsDir: 'D:/outputs' }
          if (command === 'plugin:event|listen') {
            listeners.set(args.handler, { event: args.event, handler: callbacks.get(args.handler), context })
            return args.handler
          }
          if (command === 'plugin:event|unlisten') { listeners.delete(args.eventId); return }
          if (command === 'plugin:event|emit_to') {
            if (failSend) throw new Error('event transport unavailable')
            messages.push({ label, ...clone(args) })
            await Promise.all([...listeners.values()].filter(item => item.event === args.event && item.context.label === args.target.label)
              .map(item => windows.run(item.context, () => item.handler({ payload: clone(args.payload) }))))
            return
          }
          if (['plugin:webview|set_webview_zoom', 'update_proxy_settings', 'reveal_path'].includes(command)) return
          throw new Error(`Unexpected IPC command: ${command}`)
        },
      },
    } }
    const result = await windows.run(context, async () => {
      const settings = useSettingsStore(createPinia())
      await settings.initialize()
      return { settings, run: action => windows.run(context, action) }
    })
    instances.push(result)
    return result
  }
  const main = await instance('main')
  const unlisten = await main.run(() => listenForEditorExportPreferences(main.settings.saveEditorExportPreference))
  t.after(async () => {
    await main.run(unlisten)
    for (const item of instances) item.run(() => item.settings.$dispose())
  })
  return { state, writes, messages, listeners, main, instance,
    failWrites: () => { failWrites = true }, failSend: () => { failSend = true },
    holdSave() { let release; saveGate = new Promise(resolve => { release = resolve }); return release },
  }
}

function exporter(child, exportMix = async () => ({ path: 'D:/mix.wav' })) {
  const notices = { success: [], warning: [], error: [], info: [] }
  const hook = child.run(() => useEditorExport({ settings: child.settings,
    editor: { exportFormat: 'wav', session: { id: 'project' }, exportMix },
    message: Object.fromEntries(Object.keys(notices).map(key => [key, message => notices[key].push(message)])),
    t: key => key,
  }))
  hook.openExportDialog()
  return { hook, notices }
}

test('exports leave mix gain unchanged unless peak protection is explicitly selected', async t => {
  const env = await environment(t)
  const child = await env.instance('editor-a')
  const requests = []
  const { hook } = exporter(child, async options => { requests.push(clone(options)); return { path: 'D:/mix.wav' } })
  assert.equal(hook.exportPeakProtectionDraft.value, false)
  for (const format of ['wav', 'flac']) {
    hook.setExportFormat(format)
    await child.run(hook.exportMix)
    assert.equal(requests.at(-1).audioParams.peakProtection, false)
  }
  hook.setExportPeakProtection(true)
  await child.run(hook.exportMix)
  assert.equal(requests.at(-1).audioParams.peakProtection, true)
})

for (const format of ['wav', 'flac']) {
  test(`${format} export persists through the main window and survives reopening without overwriting unrelated settings`, async t => {
    const env = await environment(t)
    const child = await env.instance('editor-a')
    const { hook } = exporter(child)
    env.main.run(() => {
      env.main.settings.proxyUrl = 'http://127.0.0.1:7002'
      env.main.settings.outputDir = 'E:/new-output'
      env.main.settings[format === 'wav' ? 'flacBitDepth' : 'wavBitDepth'] = 'PCM_24'
    })
    hook.setExportFormat(format)
    hook.setExportWavBitDepth('FLOAT')
    hook.setExportFlacBitDepth('PCM_24')
    await child.run(hook.exportMix)
    const reopened = await env.instance('editor-reopened')
    const next = exporter(reopened).hook
    assert.equal(format === 'wav' ? next.exportWavBitDepthDraft.value : next.exportFlacBitDepthDraft.value, format === 'wav' ? 'FLOAT' : 'PCM_24')
    assert.equal(env.state[format === 'wav' ? 'flacBitDepth' : 'wavBitDepth'], 'PCM_24')
    assert.equal(env.state.proxyUrl, 'http://127.0.0.1:7002')
    assert.equal(env.state.outputDir, 'E:/new-output')
    assert.ok(env.writes.length > 0)
    assert.ok(env.writes.every(write => write.label === 'main'))
    assert.equal(env.listeners.size, 1)
    // Later main-window writes must retain the child's acknowledged preference.
    await env.main.run(() => env.main.settings.markStartupOnboardingCompleted())
    assert.equal(env.state[format === 'wav' ? 'wavBitDepth' : 'flacBitDepth'], format === 'wav' ? 'FLOAT' : 'PCM_24')
  })
}

test('export waits for persistence acknowledgement, rejects duplicate submissions and remembers the values used at submission', async t => {
  const env = await environment(t)
  const child = await env.instance('editor-a')
  let finishExport
  const output = new Promise(resolve => { finishExport = resolve })
  let exportCount = 0
  const { hook } = exporter(child, () => { exportCount++; return output })
  hook.setExportWavBitDepth('FLOAT')
  const release = env.holdSave()
  let finished = false
  const exporting = child.run(hook.exportMix).then(() => { finished = true })
  hook.setExportWavBitDepth('PCM_16')
  finishExport({ path: 'D:/mix.wav' })
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(finished, false)
  await child.run(hook.exportMix)
  assert.equal(exportCount, 1)
  hook.setExportDialogVisible(false)
  hook.openExportDialog()
  assert.equal(hook.showExportDialog.value, false)
  release()
  await exporting
  assert.equal(env.state.wavBitDepth, 'FLOAT')
  assert.equal(env.listeners.size, 1)
})

test('preference persistence failure warns without reporting a successful audio export as failed', async t => {
  const env = await environment(t)
  const child = await env.instance('editor-a')
  const { hook, notices } = exporter(child)
  hook.setExportWavBitDepth('FLOAT')
  env.failWrites()
  await child.run(hook.exportMix)
  assert.deepEqual(notices.success, ['editor.exported'])
  assert.deepEqual(notices.warning, ['editor.exportPreferencesSaveFailed'])
  assert.deepEqual(notices.error, [])
  assert.equal(hook.showExportDialog.value, false)
  assert.equal(env.state.wavBitDepth, 'PCM_16')
  assert.equal(env.listeners.size, 1)
})

for (const failure of [new Error('export failed'), new EditorExportCancelledError()]) {
  test(`an unsuccessful export does not update preferences: ${failure.constructor.name}`, async t => {
    const env = await environment(t)
    const child = await env.instance('editor-a')
    const { hook } = exporter(child, async () => { throw failure })
    hook.setExportWavBitDepth('FLOAT')
    await child.run(hook.exportMix)
    assert.equal(env.state.wavBitDepth, 'PCM_16')
    assert.equal(env.messages.length, 0)
  })
}

test('invalid format or bit depth is rejected without changing the main settings', async t => {
  const env = await environment(t)
  const child = await env.instance('editor-a')
  for (const preference of [{ format: 'mp3', bitDepth: 'PCM_24' }, { format: 'flac', bitDepth: 'FLOAT' }]) {
    await assert.rejects(child.run(() => requestEditorExportPreference(preference)), /Invalid export bit depth/)
  }
  assert.equal(env.writes.length, 0)
  assert.equal(env.main.settings.wavBitDepth, 'PCM_16')
  assert.equal(env.main.settings.flacBitDepth, 'PCM_16')
  assert.equal(env.listeners.size, 1)
})

test('transport failure releases the response listener', async t => {
  const env = await environment(t)
  const child = await env.instance('editor-a')
  env.failSend()
  await assert.rejects(child.run(() => requestEditorExportPreference({ format: 'wav', bitDepth: 'FLOAT' })), /transport unavailable/)
  assert.equal(env.listeners.size, 1)
})

test('concurrent editor windows save separate format preferences without losing either update', async t => {
  const env = await environment(t)
  const first = await env.instance('editor-a')
  const second = await env.instance('editor-b')
  await Promise.all([
    first.run(() => first.settings.saveEditorExportPreference({ format: 'wav', bitDepth: 'FLOAT' })),
    second.run(() => second.settings.saveEditorExportPreference({ format: 'flac', bitDepth: 'PCM_24' })),
  ])
  assert.equal(env.state.wavBitDepth, 'FLOAT')
  assert.equal(env.state.flacBitDepth, 'PCM_24')
  assert.equal(first.settings.wavBitDepth, 'FLOAT')
  assert.equal(second.settings.flacBitDepth, 'PCM_24')
  assert.ok(env.writes.every(write => write.label === 'main'))
  assert.equal(env.listeners.size, 1)
})

test('a missing main window times out and releases the response listener', async t => {
  const env = await environment(t)
  const child = await env.instance('editor-a')
  env.listeners.clear()
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const saving = child.run(() => requestEditorExportPreference({ format: 'wav', bitDepth: 'FLOAT' }))
  const rejected = assert.rejects(saving, /timed out/)
  await new Promise(resolve => setImmediate(resolve))
  t.mock.timers.tick(10000)
  await rejected
  assert.equal(env.listeners.size, 0)
})
