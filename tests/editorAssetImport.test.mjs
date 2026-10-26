import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createPinia } from 'pinia'
import { watch } from 'vue'

globalThis.window = { matchMedia: () => ({ matches: false }), addEventListener() {}, removeEventListener() {} }
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { language: 'en' } })
const vite = await createServer({
  configFile: false,
  server: { watch: null, middlewareMode: true, hmr: false, preTransformRequests: false },
  appType: 'custom', optimizeDeps: { noDiscovery: true },
  resolve: { alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) } },
})
after(() => vite.close())
const { useEditorStore } = await vite.ssrLoadModule('/src/stores/editor.ts')
const flush = () => new Promise(resolve => setImmediate(resolve))
const clone = value => JSON.parse(JSON.stringify(value))
const asset = path => ({ path, name: path.split('/').at(-1), originKind: 'external', originRoot: 'D:/audio', relativePath: path.split('/').at(-1), missing: false })
const metadata = path => ({ path, name: path.split('/').at(-1), duration: 12, sampleRate: 48000, channels: 2 })

function deferred() {
  let resolve
  let reject
  const promise = new Promise((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

function project(id = 'project-a') {
  return {
    id, name: id, masterVolume: 1, masterPan: 0, createdAt: 1, updatedAt: 1,
    sources: [{ id: 'existing-source', path: 'D:/existing.wav', name: 'existing.wav', role: 'stem', duration: 8, sampleRate: 48000, channels: 2, missing: false }],
    tracks: [{ id: 'existing-track', sourceId: 'existing-source', role: 'stem', name: 'Existing track', volume: 0.8, pan: 0,
      muted: false, solo: false, fadeIn: 0, fadeOut: 0,
      clips: [{ id: 'existing-clip', assetId: 'existing-source', start: 2, offset: 0, duration: 4, fadeIn: 0, fadeOut: 0 }] }],
  }
}

function environment(t, { importFiles, metadataForPath = metadata } = {}) {
  const calls = []
  const saves = []
  window.__TAURI_INTERNALS__ = {
    async invoke(command, args) {
      calls.push({ command, args })
      if (command === 'import_editor_assets') {
        return importFiles ? importFiles(args) : { files: args.paths.map(asset), warnings: [] }
      }
      if (command === 'get_audio_metadata') return metadataForPath(args.payload.path)
      if (command === 'generate_waveform_peaks') return {
        ...metadata(args.payload.path), peaks: [0, 0.25], channelPeaks: [[0, 0.25], [0, 0.25]], peaksPath: null,
      }
      if (command === 'save_editor_project') { saves.push(clone(args.project)); return args.project }
      throw new Error(`Unexpected IPC command: ${command}`)
    },
  }
  const store = useEditorStore(createPinia())
  store.session = project()
  store.selectedTrackId = 'existing-track'
  store.selectedClipId = 'existing-clip'
  store.clearHistory()
  t.after(async () => { await flush(); await store.flushSave(); store.$dispose() })
  return { store, calls, saves }
}

test('ordinary asset import only updates the library and preserves import provenance', async t => {
  const { store, calls, saves } = environment(t)
  const result = await store.scanAssets(['D:/audio/first.wav', 'D:/audio/second.wav'])
  assert.deepEqual(result.files, ['D:/audio/first.wav', 'D:/audio/second.wav'])
  assert.equal(store.session.sources.length, 3)
  assert.equal(store.session.tracks.length, 1)
  assert.equal(store.canUndo, false)
  assert.equal(store.session.sources[1].originKind, 'external')
  assert.equal(store.session.sources[1].relativePath, 'first.wav')
  assert.equal(store.session.sources[1].originRoot, 'D:/audio')
  assert.deepEqual(calls.find(call => call.command === 'import_editor_assets').args, {
    projectId: 'project-a', paths: ['D:/audio/first.wav', 'D:/audio/second.wav'],
  })
  assert.equal(saves.at(-1).sources.length, 3)
  assert.equal(saves.at(-1).tracks.length, 1)
})

test('multi-file timeline import creates one reference track per source at zero and one undo for the batch', async t => {
  const { store, saves } = environment(t)
  const existing = clone(store.session.tracks[0])
  await store.scanAssets(['D:/audio/first.wav', 'D:/audio/second.wav'], { addTracks: true })
  assert.equal(store.session.sources.length, 3)
  assert.equal(store.session.tracks.length, 3)
  assert.deepEqual(clone(store.session.tracks[0]), existing)
  for (const track of store.session.tracks.slice(1)) {
    assert.equal(track.role, 'reference')
    assert.equal(track.clips.length, 1)
    assert.equal(track.clips[0].start, 0)
    assert.equal(track.clips[0].duration, 12)
    assert.equal(track.clips[0].assetId, track.sourceId)
    assert.ok(store.session.sources.some(source => source.id === track.sourceId))
  }
  assert.equal(saves.at(-1).tracks.length, 3)
  store.undo()
  assert.deepEqual(clone(store.session.tracks), [existing])
  assert.equal(store.session.sources.length, 3, 'undoing track placement must keep imported library assets')
  assert.equal(store.selectedTrackId, 'existing-track')
  assert.equal(store.canUndo, false)
  store.redo()
  assert.equal(store.session.tracks.length, 3)
})

test('repeated drops reuse existing library sources but add independent reference tracks', async t => {
  const { store, calls } = environment(t)
  await store.scanAssets(['D:/existing.wav'], { addTracks: true })
  await store.scanAssets(['D:/existing.wav'], { addTracks: true })
  assert.equal(store.session.sources.length, 1)
  assert.equal(store.session.tracks.length, 3)
  assert.ok(store.session.tracks.every(track => track.sourceId === 'existing-source'))
  assert.equal(calls.filter(call => call.command === 'get_audio_metadata').length, 0)
  store.undo()
  assert.equal(store.session.tracks.length, 2)
  store.undo()
  assert.equal(store.session.tracks.length, 1)
  assert.equal(store.canUndo, false)
})

test('duplicate scan results create one library source and one reference track', async t => {
  const { store } = environment(t, { importFiles: () => ({ files: [asset('D:/audio/first.wav'), asset('D:/audio/first.wav')], warnings: [] }) })
  await store.scanAssets(['D:/audio/first.wav'], { addTracks: true })
  assert.equal(store.session.sources.length, 2)
  assert.equal(store.session.tracks.length, 2)
  store.undo()
  assert.equal(store.session.tracks.length, 1)
  assert.equal(store.canUndo, false)
})

test('an empty import result leaves tracks, library and undo history unchanged', async t => {
  const { store, calls } = environment(t, { importFiles: () => ({ files: [], warnings: ['unsupported file'] }) })
  const original = clone(store.session)
  const result = await store.scanAssets(['D:/audio/readme.txt'], { addTracks: true })
  assert.deepEqual(result, { files: [], warnings: ['unsupported file'] })
  assert.deepEqual(clone(store.session), original)
  assert.equal(store.canUndo, false)
  assert.equal(calls.filter(call => call.command === 'get_audio_metadata').length, 0)
})

test('switching projects while native import is pending rejects the drop without touching the new project', async t => {
  const gate = deferred()
  const { store, calls } = environment(t, { importFiles: () => gate.promise })
  const importing = store.scanAssets(['D:/audio/first.wav'], { addTracks: true })
  store.session = project('project-b')
  const replacement = clone(store.session)
  gate.resolve({ files: [asset('D:/audio/first.wav')], warnings: [] })
  await assert.rejects(importing, /project changed/i)
  assert.deepEqual(clone(store.session), replacement)
  assert.equal(store.canUndo, false)
  assert.equal(calls.filter(call => call.command === 'get_audio_metadata').length, 0)
})

test('reloading the same project during metadata loading cannot attach assets to its replacement session', async t => {
  const gate = deferred()
  const { store, calls } = environment(t, { metadataForPath: () => gate.promise })
  const importing = store.scanAssets(['D:/audio/first.wav'], { addTracks: true })
  await flush()
  assert.equal(calls.filter(call => call.command === 'get_audio_metadata').length, 1)
  store.session = { ...project(), name: 'Reloaded project' }
  const replacement = clone(store.session)
  gate.resolve(metadata('D:/audio/first.wav'))
  await assert.rejects(importing, /project changed/i)
  assert.deepEqual(clone(store.session), replacement)
  assert.equal(store.canUndo, false)
  assert.equal(calls.filter(call => call.command === 'generate_waveform_peaks').length, 0)
})

test('switching at the first source completion prevents later batch files from entering the new project', async t => {
  const { store, calls } = environment(t)
  let switched = false
  let replacement
  const stop = watch(() => store.session.sources.length, count => {
    if (switched || count !== 2) return
    switched = true
    // Complete the current ensureSourceByPath call, then replace the project
    // before scanAssets resumes its await and starts importing the next file.
    queueMicrotask(() => {
      store.session = project('project-b')
      replacement = clone(store.session)
    })
  }, { flush: 'sync' })
  t.after(stop)
  await assert.rejects(store.scanAssets(['D:/audio/first.wav', 'D:/audio/second.wav'], { addTracks: true }), /project changed/i)
  assert.equal(switched, true)
  assert.deepEqual(clone(store.session), replacement)
  assert.deepEqual(calls.filter(call => call.command === 'get_audio_metadata').map(call => call.args.payload.path), ['D:/audio/first.wav'])
  assert.equal(store.canUndo, false)
})

test('native import failures create no assets, tracks or history', async t => {
  const { store } = environment(t, { importFiles: () => { throw new Error('File access denied') } })
  const original = clone(store.session)
  await assert.rejects(store.scanAssets(['D:/audio/first.wav'], { addTracks: true }), /File access denied/)
  assert.deepEqual(clone(store.session), original)
  assert.equal(store.canUndo, false)
})

test('concurrent metadata reads for the same path converge on a single library source', async t => {
  const gates = [deferred(), deferred()]
  let request = 0
  const { store } = environment(t, { metadataForPath: () => gates[request++].promise })
  const first = store.scanAssets(['D:/audio/first.wav'])
  const second = store.scanAssets(['D:/audio/first.wav'])
  await flush()
  assert.equal(request, 2)
  gates[1].resolve(metadata('D:/audio/first.wav'))
  await second
  gates[0].resolve(metadata('D:/audio/first.wav'))
  await first
  assert.equal(store.session.sources.length, 2)
  assert.equal(store.session.tracks.length, 1)
  assert.equal(store.canUndo, false)
})

test('timeline import waits for an active volume gesture and keeps their undo actions separate', async t => {
  const gate = deferred()
  const { store } = environment(t, { metadataForPath: () => gate.promise })
  let finished = false
  const importing = store.scanAssets(['D:/audio/first.wav'], { addTracks: true }).finally(() => { finished = true })
  await flush()
  store.beginInteraction()
  store.setMasterVolume(0.6)
  let dragging = true
  try {
    gate.resolve(metadata('D:/audio/first.wav'))
    await flush()
    assert.equal(finished, false)
    assert.equal(store.session.tracks.length, 1)
    store.setMasterVolume(0.7)
    store.commitInteraction()
    dragging = false
    await importing
    assert.equal(store.session.tracks.length, 2)
    assert.equal(store.masterVolume, 0.7)
    store.undo()
    assert.equal(store.session.tracks.length, 1)
    assert.equal(store.masterVolume, 0.7, 'the first undo removes only the imported track')
    store.undo()
    assert.equal(store.masterVolume, 1)
    assert.equal(store.session.sources.length, 2)
    assert.equal(store.canUndo, false)
  } finally {
    if (dragging) store.commitInteraction()
    await importing.catch(() => {})
  }
})

test('switching sessions while import waits for a gesture rejects promptly and allows later imports', async t => {
  const gate = deferred()
  const { store } = environment(t, { metadataForPath: path => path.endsWith('first.wav') ? gate.promise : metadata(path) })
  let finished = false
  const importing = store.scanAssets(['D:/audio/first.wav'], { addTracks: true }).finally(() => { finished = true })
  await flush()
  store.beginInteraction()
  store.setMasterVolume(0.6)
  try {
    gate.resolve(metadata('D:/audio/first.wav'))
    await flush()
    assert.equal(finished, false)
    assert.equal(store.session.tracks.length, 1)
    store.session = project('project-b')
    const replacement = clone(store.session)
    await assert.rejects(importing, /project changed/i)
    assert.deepEqual(clone(store.session), replacement)
  } finally {
    store.commitInteraction()
    await importing.catch(() => {})
  }
  store.clearHistory()
  await store.scanAssets(['D:/audio/second.wav'], { addTracks: true })
  assert.equal(store.session.id, 'project-b')
  assert.equal(store.session.tracks.length, 2)
  assert.equal(store.session.sources.length, 2)
  assert.ok(store.session.sources.some(source => source.path === 'D:/audio/second.wav'))
  assert.ok(!store.session.sources.some(source => source.path === 'D:/audio/first.wav'))
  store.undo()
  assert.equal(store.session.tracks.length, 1)
  assert.equal(store.canUndo, false)
})

test('metadata rejection returns the original error and creates no source, track or history', async t => {
  for (const error of [new Error('Audio header could not be decoded'), 'Metadata worker exited unexpectedly']) {
    await t.test(String(error), async t => {
      const { store, calls } = environment(t, { metadataForPath: () => Promise.reject(error) })
      const original = clone(store.session)
      const result = await store.scanAssets(['D:/audio/broken.wav'], { addTracks: true })
      assert.deepEqual(result.files, [])
      assert.equal(result.warnings.length, 1)
      assert.ok(result.warnings[0].includes('broken.wav'))
      assert.ok(result.warnings[0].includes(error instanceof Error ? error.message : error))
      assert.deepEqual(clone(store.session), original)
      assert.equal(store.canUndo, false)
      assert.equal(calls.filter(call => call.command === 'generate_waveform_peaks').length, 0)
    })
  }
})

test('mixed validity batches import only usable files and keep one undo for their tracks', async t => {
  const backendWarning = 'unsupported file: D:/audio/readme.txt'
  const { store } = environment(t, {
    importFiles: args => ({ files: args.paths.map(asset), warnings: [backendWarning] }),
    metadataForPath: path => path.endsWith('broken.wav')
      ? Promise.reject(new Error('Audio decoder rejected the stream')) : metadata(path),
  })
  const originalTrack = clone(store.session.tracks[0])
  const result = await store.scanAssets(['D:/audio/first.wav', 'D:/audio/broken.wav', 'D:/audio/second.wav'], { addTracks: true })
  assert.deepEqual(result.files, ['D:/audio/first.wav', 'D:/audio/second.wav'])
  assert.ok(result.warnings.includes(backendWarning))
  assert.ok(result.warnings.some(warning => warning.includes('broken.wav') && warning.includes('Audio decoder rejected the stream')))
  assert.equal(store.session.sources.length, 3)
  assert.equal(store.session.tracks.length, 3)
  assert.ok(!store.session.sources.some(source => source.path.endsWith('broken.wav')))
  for (const track of store.session.tracks.slice(1)) {
    assert.equal(track.clips[0].duration, 12)
    assert.equal(track.clips[0].start, 0)
  }
  store.undo()
  assert.deepEqual(clone(store.session.tracks), [originalTrack])
  assert.equal(store.session.sources.length, 3)
  assert.equal(store.canUndo, false)
})

test('non-positive and non-finite metadata fields are rejected before library or track creation', async t => {
  for (const field of ['duration', 'sampleRate', 'channels']) {
    for (const value of [0, NaN, Infinity]) {
      await t.test(`${field}=${value}`, async t => {
        const { store, calls } = environment(t, { metadataForPath: path => ({ ...metadata(path), [field]: value }) })
        const original = clone(store.session)
        const result = await store.scanAssets(['D:/audio/invalid.wav'], { addTracks: true })
        assert.deepEqual(result.files, [])
        assert.equal(result.warnings.length, 1)
        assert.ok(result.warnings[0].includes('invalid.wav'))
        assert.deepEqual(clone(store.session), original)
        assert.equal(store.canUndo, false)
        assert.equal(calls.filter(call => call.command === 'generate_waveform_peaks').length, 0)
      })
    }
  }
})

test('cached invalid or missing library sources cannot produce new reference tracks', async t => {
  for (const [name, patch] of [
    ['missing', { missing: true }], ['duration', { duration: 0 }],
    ['sampleRate', { sampleRate: NaN }], ['channels', { channels: 0 }],
  ]) {
    await t.test(name, async t => {
      const { store } = environment(t)
      Object.assign(store.session.sources[0], patch)
      const original = clone(store.session)
      const result = await store.scanAssets(['D:/existing.wav'], { addTracks: true })
      assert.deepEqual(result.files, [])
      assert.equal(result.warnings.length, 1)
      assert.ok(result.warnings[0].includes('existing.wav'))
      assert.deepEqual(clone(store.session), original)
      assert.equal(store.canUndo, false)
    })
  }
})

test('a failed metadata import can be retried at the same path after the decoder recovers', async t => {
  let decoderAvailable = false
  const { store, calls } = environment(t, {
    metadataForPath: path => decoderAvailable ? metadata(path) : Promise.reject(new Error('Audio decoder temporarily unavailable')),
  })
  const failed = await store.scanAssets(['D:/audio/retry.wav'], { addTracks: true })
  assert.deepEqual(failed.files, [])
  assert.equal(store.session.sources.length, 1)
  assert.equal(store.session.tracks.length, 1)
  assert.equal(store.canUndo, false)
  decoderAvailable = true
  const recovered = await store.scanAssets(['D:/audio/retry.wav'], { addTracks: true })
  assert.deepEqual(recovered, { files: ['D:/audio/retry.wav'], warnings: [] })
  assert.equal(store.session.sources.length, 2)
  assert.equal(store.session.tracks.length, 2)
  assert.equal(store.session.tracks[1].clips[0].duration, 12)
  assert.equal(calls.filter(call => call.command === 'get_audio_metadata').length, 2)
  store.undo()
  assert.equal(store.session.tracks.length, 1)
  assert.equal(store.canUndo, false)
})

test('removing a source during batch metadata loading skips it and keeps later files', async t => {
  for (const addTracks of [false, true]) {
    await t.test(addTracks ? 'timeline drop' : 'library drop', async t => {
      const gate = deferred()
      const entered = deferred()
      const { store, saves } = environment(t, {
        metadataForPath: path => {
          if (!path.endsWith('third.wav')) return metadata(path)
          entered.resolve()
          return gate.promise
        },
      })
      const originalTrack = clone(store.session.tracks[0])
      const paths = ['D:/audio/first.wav', 'D:/audio/second.wav', 'D:/audio/third.wav']
      const importing = store.scanAssets(paths, { addTracks })
      await entered.promise
      const removed = store.session.sources.find(source => source.path === paths[1])
      assert.ok(removed)
      store.removeSource(removed.id)
      gate.resolve(metadata(paths[2]))
      const result = await importing
      assert.deepEqual(result.files, [paths[0], paths[2]])
      assert.equal(result.warnings.length, 1)
      assert.ok(result.warnings[0].includes('second.wav'))
      assert.ok(!store.session.sources.some(source => source.id === removed.id))
      assert.equal(store.session.tracks.length, addTracks ? 3 : 1)
      assert.equal(saves.at(-1).tracks.length, addTracks ? 3 : 1)
      if (addTracks) {
        assert.deepEqual(store.session.tracks.slice(1).map(track => track.name), ['first', 'third'])
        store.undo()
        assert.deepEqual(clone(store.session.tracks), [originalTrack])
      }
    })
  }
})

test('removing all pending sources before a drag finishes adds no empty import undo action', async t => {
  const { store } = environment(t)
  store.beginInteraction()
  store.setMasterVolume(0.7)
  const importing = store.scanAssets(['D:/audio/first.wav', 'D:/audio/second.wav'], { addTracks: true })
  await flush()
  const removed = store.session.sources.filter(source => source.role === 'reference')
  assert.equal(removed.length, 2)
  for (const source of removed) store.removeSource(source.id)
  store.commitInteraction()
  const result = await importing
  assert.deepEqual(result.files, [])
  assert.equal(result.warnings.length, 2)
  assert.equal(store.session.sources.length, 1)
  assert.equal(store.session.tracks.length, 1)
  store.undo()
  assert.equal(store.masterVolume, 1)
  assert.equal(store.canUndo, false, 'the import must not add history when every pending source was removed')
})

test('a source that becomes unavailable while waiting for a drag cannot produce a track', async t => {
  const { store } = environment(t)
  store.beginInteraction()
  store.setMasterVolume(0.7)
  const importing = store.scanAssets(['D:/audio/first.wav'], { addTracks: true })
  await flush()
  const source = store.session.sources.find(source => source.role === 'reference')
  assert.ok(source)
  source.missing = true
  store.commitInteraction()
  const result = await importing
  assert.deepEqual(result.files, [])
  assert.equal(result.warnings.length, 1)
  assert.equal(store.session.tracks.length, 1)
  store.undo()
  assert.equal(store.masterVolume, 1)
  assert.equal(store.canUndo, false)
})
