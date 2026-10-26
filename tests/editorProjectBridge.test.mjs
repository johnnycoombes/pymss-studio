import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'

const source = ts.createSourceFile('bridge.ts', readFileSync(new URL('../src/composables/useEditorProjectBridge.ts', import.meta.url), 'utf8'), ts.ScriptTarget.Latest, true)
const declaration = source.statements.find(item => ts.isFunctionDeclaration(item) && item.name?.text === 'useEditorProjectBridge')
const code = ts.transpileModule(declaration.getText(source).replace(/^export /, ''), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
const deferred = () => {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}

function environment({ scale = 1, platform = '', zoom = 1 } = {}) {
  let mounted, unmount, projectEvent, dropEvent
  const calls = []
  const errors = []
  const notices = []
  const translations = []
  const assetPanelEl = { value: null }
  const mixerRef = { value: null }
  let canDropToMixer = true
  const editor = {
    session: null,
    loading: false,
    async loadProject(id) { calls.push(['load', id]); editor.session = { id, volume: 1 } },
    async saveProject() { calls.push(['save', editor.session.id, editor.session.volume]) },
    async scanAssets(paths, options) { calls.push(['scan', paths, { ...options }]); return { files: paths, warnings: [] } },
  }
  let dropSubscribe = async callback => { dropEvent = callback; return () => calls.push(['remove-drop']) }
  let projectSubscribe = async callback => { projectEvent = callback; return () => calls.push(['remove-project']) }
  const context = {
    Error,
    ref: value => ({ value }),
    watch() {},
    onMounted: callback => { mounted = callback },
    onBeforeUnmount: callback => { unmount = callback },
    getCurrentWebview: () => ({ onDragDropEvent: callback => dropSubscribe(callback) }),
    listen: (_event, callback) => projectSubscribe(callback),
    window: { devicePixelRatio: scale, navigator: { platform } },
  }
  const create = vm.runInNewContext(`${code}\nuseEditorProjectBridge`, context)
  const bridge = create({ routeProjectId: { value: '' }, hasTauriApis: true, editor, assetPanelEl, mixerRef,
    canDropToMixer: () => canDropToMixer,
    zoomFactor: { value: zoom },
    message: { error: value => errors.push(value), success: value => notices.push(['success', value]), warning: value => notices.push(['warning', value]) },
    t: (key, values) => { translations.push([key, values && { ...values }]); return key } })
  return { bridge, editor, calls, errors, mount: () => mounted(), unmount: () => unmount(),
    notices, translations, assetPanelEl, mixerRef, disableMixer: () => { canDropToMixer = false },
    drop: payload => dropEvent({ payload }),
    event: id => projectEvent({ payload: { projectId: id } }),
    setDrop: callback => { dropSubscribe = callback }, setProject: callback => { projectSubscribe = callback } }
}

test('reopening the current audio project preserves edits and pending autosave', async () => {
  const env = environment()
  await env.mount()
  await env.bridge.loadProject('project')
  env.editor.session.volume = 0.25
  env.event('project')
  await env.bridge.loadProject('project')
  assert.equal(env.editor.session.volume, 0.25)
  assert.deepEqual(env.calls, [['load', 'project']])
  env.unmount()
})

test('queued duplicate opens load once and switching projects saves the current draft first', async () => {
  const env = environment()
  await Promise.all([env.bridge.loadProject('first'), env.bridge.loadProject('first')])
  env.editor.session.volume = 0.3
  await env.bridge.loadProject('second')
  assert.deepEqual(env.calls, [['load', 'first'], ['save', 'first', 0.3], ['load', 'second']])
})

test('a failed save prevents switching projects and a later request can retry', async () => {
  const env = environment()
  await env.bridge.loadProject('first')
  env.editor.saveProject = async () => { throw new Error('disk full') }
  await assert.rejects(env.bridge.loadProject('second'), /disk full/)
  assert.equal(env.editor.session.id, 'first')
  env.editor.saveProject = async () => {}
  await env.bridge.loadProject('second')
  assert.equal(env.editor.session.id, 'second')
})

test('an asynchronous drop subscription is removed when the view closes before it resolves', async () => {
  const env = environment()
  const waiting = deferred()
  env.setDrop(() => waiting.promise)
  const mounting = env.mount()
  env.unmount()
  waiting.resolve(() => env.calls.push(['late-drop-removed']))
  await mounting
  await env.bridge.loadProject('project')
  assert.deepEqual(env.calls, [['late-drop-removed']])
})

test('an asynchronous project subscription is removed when the view closes before it resolves', async () => {
  const env = environment()
  const waiting = deferred()
  env.setProject(() => waiting.promise)
  const mounting = env.mount()
  await new Promise(resolve => setImmediate(resolve))
  env.unmount()
  waiting.resolve(() => env.calls.push(['late-project-removed']))
  await mounting
  assert.deepEqual(env.calls, [['remove-drop'], ['late-project-removed']])
})

function prepareDropTargets(env) {
  env.editor.session = { id: 'project' }
  env.assetPanelEl.value = { getBoundingClientRect: () => ({ left: 0, right: 200, top: 50, bottom: 500, width: 200, height: 450 }) }
  env.mixerRef.value = { containsPoint: (x, y) => x >= 210 && x <= 800 && y >= 50 && y <= 500 }
}

test('external hover and drop use scaled coordinates and distinguish the library from the timeline', async () => {
  const env = environment({ scale: 2 })
  prepareDropTargets(env)
  await env.mount()
  await env.drop({ type: 'enter', position: { x: 200, y: 200 } })
  assert.equal(env.bridge.isDraggingExternal.value, true)
  assert.equal(env.bridge.isDraggingExternalOnMixer.value, false)
  await env.drop({ type: 'drop', position: { x: 200, y: 200 }, paths: ['a.wav'] })
  assert.deepEqual(env.calls[0], ['scan', ['a.wav'], { addTracks: false }])
  assert.deepEqual(env.notices[0], ['success', 'editor.importSuccess'])
  await env.drop({ type: 'over', position: { x: 1000, y: 200 } })
  assert.equal(env.bridge.isDraggingExternal.value, false)
  assert.equal(env.bridge.isDraggingExternalOnMixer.value, true)
  await env.drop({ type: 'drop', position: { x: 1000, y: 200 }, paths: ['a.wav', 'b.flac'] })
  assert.deepEqual(env.calls[1], ['scan', ['a.wav', 'b.flac'], { addTracks: true }])
  assert.deepEqual(env.notices[1], ['success', 'editor.importTracksSuccess'])
  assert.equal(env.bridge.isDraggingExternalOnMixer.value, false)
  env.unmount()
})

test('leaving or dropping outside the editor clears the hover without importing', async () => {
  const env = environment()
  prepareDropTargets(env)
  await env.mount()
  await env.drop({ type: 'over', position: { x: 300, y: 100 } })
  assert.equal(env.bridge.isDraggingExternalOnMixer.value, true)
  await env.drop({ type: 'leave' })
  assert.equal(env.bridge.isDraggingExternalOnMixer.value, false)
  await env.drop({ type: 'drop', position: { x: 900, y: 100 }, paths: ['a.wav'] })
  await env.drop({ type: 'drop', position: { x: 300, y: 100 }, paths: [] })
  assert.deepEqual(env.calls, [])
  env.unmount()
})

test('macOS Retina drops use Cocoa points and apply only the webview zoom', async () => {
  for (const zoom of [1, 1.25]) {
    const env = environment({ scale: 2, platform: 'MacIntel', zoom })
    prepareDropTargets(env)
    await env.mount()
    await env.drop({ type: 'over', position: { x: 300 * zoom, y: 100 * zoom } })
    assert.equal(env.bridge.isDraggingExternalOnMixer.value, true)
    assert.equal(env.bridge.isDraggingExternal.value, false)
    await env.drop({ type: 'drop', position: { x: 300 * zoom, y: 100 * zoom }, paths: ['a.wav'] })
    assert.deepEqual(env.calls[0], ['scan', ['a.wav'], { addTracks: true }])
    env.unmount()
  }
})

test('loading, missing projects and recording reject timeline drops', async () => {
  const env = environment()
  prepareDropTargets(env)
  await env.mount()
  env.editor.loading = true
  await env.drop({ type: 'drop', position: { x: 300, y: 100 }, paths: ['a.wav'] })
  env.editor.loading = false
  env.editor.session = null
  await env.drop({ type: 'drop', position: { x: 300, y: 100 }, paths: ['a.wav'] })
  env.editor.session = { id: 'project' }
  env.disableMixer()
  await env.drop({ type: 'over', position: { x: 300, y: 100 } })
  assert.equal(env.bridge.isDraggingExternalOnMixer.value, false)
  await env.drop({ type: 'drop', position: { x: 300, y: 100 }, paths: ['a.wav'] })
  assert.deepEqual(env.calls, [])
  // Recording does not prevent adding assets to the library.
  await env.drop({ type: 'drop', position: { x: 100, y: 100 }, paths: ['a.wav'] })
  assert.deepEqual(env.calls[0], ['scan', ['a.wav'], { addTracks: false }])
  env.unmount()
})

test('pending imports expose progress and reject overlapping drops', async () => {
  const env = environment()
  prepareDropTargets(env)
  await env.mount()
  const gate = deferred()
  let imports = 0
  env.editor.scanAssets = async () => { imports++; return gate.promise }
  const first = env.drop({ type: 'drop', position: { x: 300, y: 100 }, paths: ['a.wav'] })
  assert.equal(env.bridge.isImportingExternal.value, true)
  await env.drop({ type: 'over', position: { x: 300, y: 100 } })
  assert.equal(env.bridge.isDraggingExternalOnMixer.value, false)
  await env.drop({ type: 'drop', position: { x: 300, y: 100 }, paths: ['b.wav'] })
  assert.equal(imports, 1)
  gate.resolve({ files: ['a.wav'], warnings: [] })
  await first
  assert.equal(env.bridge.isImportingExternal.value, false)
  assert.deepEqual(env.notices, [['success', 'editor.importTracksSuccess']])
  env.unmount()
})

test('empty and failed imports report their result and allow retry', async () => {
  const env = environment()
  prepareDropTargets(env)
  await env.mount()
  env.editor.scanAssets = async () => ({ files: [], warnings: [] })
  await env.drop({ type: 'drop', position: { x: 300, y: 100 }, paths: ['a.txt'] })
  assert.deepEqual(env.notices, [['warning', 'editor.importEmpty']])
  env.editor.scanAssets = async () => { throw new Error('Permission denied') }
  await env.drop({ type: 'drop', position: { x: 300, y: 100 }, paths: ['a.wav'] })
  assert.deepEqual(env.errors, ['Permission denied'])
  assert.equal(env.bridge.isImportingExternal.value, false)
  env.unmount()
})

test('late import completion after a project switch or unmount produces no stale notifications', async () => {
  for (const action of ['switch', 'unmount']) {
    const env = environment()
    prepareDropTargets(env)
    await env.mount()
    const gate = deferred()
    env.editor.scanAssets = async () => gate.promise
    const pending = env.drop({ type: 'drop', position: { x: 300, y: 100 }, paths: ['a.wav'] })
    if (action === 'switch') env.editor.session = { id: 'other-project' }
    else env.unmount()
    gate.resolve({ files: ['a.wav'], warnings: [] })
    await pending
    assert.deepEqual(env.notices, [])
    assert.deepEqual(env.errors, [])
    assert.equal(env.bridge.isImportingExternal.value, false)
    if (action === 'switch') env.unmount()
  }
})

test('a partially successful import reports the successful files and preserves failure details', async () => {
  const env = environment()
  prepareDropTargets(env)
  await env.mount()
  env.editor.scanAssets = async () => ({ files: ['good.wav'], warnings: ['broken.wav: Invalid audio data'] })
  await env.drop({ type: 'drop', position: { x: 300, y: 100 }, paths: ['good.wav', 'broken.wav'] })
  assert.deepEqual(env.notices, [['success', 'editor.importTracksSuccess'], ['warning', 'editor.importWarnings']])
  assert.deepEqual(env.translations, [
    ['editor.importTracksSuccess', { count: 1 }],
    ['editor.importWarnings', { detail: 'broken.wav: Invalid audio data' }],
  ])
  assert.equal(env.bridge.isImportingExternal.value, false)
  env.unmount()
})

test('an import with no readable audio reports the original failure without a success notification', async () => {
  const env = environment()
  prepareDropTargets(env)
  await env.mount()
  env.editor.scanAssets = async () => ({ files: [], warnings: ['broken.wav: Access denied'] })
  await env.drop({ type: 'drop', position: { x: 300, y: 100 }, paths: ['broken.wav'] })
  assert.deepEqual(env.notices, [['warning', 'editor.importWarnings']])
  assert.deepEqual(env.translations, [['editor.importWarnings', { detail: 'broken.wav: Access denied' }]])
  assert.equal(env.bridge.isImportingExternal.value, false)
  env.unmount()
})
