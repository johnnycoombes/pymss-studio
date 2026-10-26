import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { computed, effectScope, nextTick, reactive, ref, watch } from 'vue'
import { createPinia } from 'pinia'
import { useEditorPlaybackStore } from '../src/stores/editorPlayback.ts'
import { hasEditorPlugins } from '../src/utils/editorPlugins.ts'

const source = ts.createSourceFile('recording.ts', readFileSync(new URL('../src/composables/useEditorRecording.ts', import.meta.url), 'utf8'), ts.ScriptTarget.Latest, true)
const code = ts.transpileModule(source.statements.filter(statement => !ts.isImportDeclaration(statement))
  .map(statement => statement.getText(source)).join('\n').replace('export function useEditorRecording', 'function useEditorRecording'),
  { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None } }).outputText
const flush = async () => { for (let index = 0; index < 3; index++) { await nextTick(); await new Promise(resolve => setImmediate(resolve)) } }
function deferred() { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }

function environment(t, { plugins = true, capability = true, playGate, permissionGate, finalizeGate, appendGate, appendFailure, playResult = true } = {}) {
  const playback = useEditorPlaybackStore(createPinia())
  playback.setCurrentTime(2)
  playback.setLoop(true)
  const editor = reactive({ session: { id: 'project', tracks: [{ id: 'take', role: 'recording', pluginChain: [] }],
    pluginChain: plugins ? [{ id: 'effect', format: 'vst3', pluginId: 'gain', path: '/gain.vst3', bypassed: false, state: 'saved' }] : [] },
    get selectedTrack() { return this.session.tracks[0] },
    addRecordingTrack() { throw new Error('The selected recording track must be reused') },
    async addRecordingClip(id, result, start) { calls.push(['attach', id, result, start]) },
    async flushSave() { calls.push(['save']) },
  })
  const calls = [], processors = [], streams = [], contexts = [], unmount = [], frames = new Map(), files = new Map()
  let now = 0, frameId = 0, fileId = 0
  const node = () => ({ gain: { value: 1 }, connect() {}, disconnect() {} })
  class AudioContext {
    sampleRate = 48000
    state = 'running'
    destination = node()
    constructor() { contexts.push(this) }
    async resume() {}
    async close() { this.state = 'closed' }
    createMediaStreamSource() { return node() }
    createGain() { return node() }
    createScriptProcessor() { const processor = { ...node(), onaudioprocess: null }; processors.push(processor); return processor }
  }
  const create = vm.runInNewContext(`${code}\nuseEditorRecording`, {
    computed, ref, watch, hasEditorPlugins, useEditorPlaybackStore: () => playback,
    onBeforeUnmount: action => unmount.push(action), registerWindowCloseGuard: () => () => {},
    i18n: { global: { t: key => key } },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    navigator: { mediaDevices: {
      async enumerateDevices() { return [] }, addEventListener() {}, removeEventListener() {},
      async getUserMedia() { calls.push(['permission']); const track = { stops: 0, stop() { this.stops++ } }; const stream = { getTracks: () => [track], track }; streams.push(stream); if (permissionGate) await permissionGate.promise; return stream },
    } },
    window: { AudioContext }, performance: { now: () => now },
    requestAnimationFrame: callback => { frames.set(++frameId, callback); return frameId }, cancelAnimationFrame: id => frames.delete(id),
    btoa, Uint8Array, DataView,
    async pluginHostCapabilities() { calls.push(['capability']); return { enabled: true, ...(capability === null ? {} : { recording: capability }) } },
    async invoke(command, args) {
      calls.push([command, args])
      if (command === 'begin_editor_recording') { const id = `capture-${++fileId}`; files.set(id, []); return { recordingId: id } }
      if (command === 'append_editor_recording_chunk') {
        if (appendGate) await appendGate.promise
        if (appendFailure) throw new Error(appendFailure)
        files.get(args.recordingId).push(Buffer.from(args.dataBase64, 'base64'))
      }
      if (command === 'finish_editor_recording') { if (finalizeGate) await finalizeGate.promise; return { path: '/take.wav', name: 'Take', sampleRate: args.sampleRate, channels: 1, duration: Buffer.concat(files.get(args.recordingId)).length / 2 / args.sampleRate } }
    },
  })
  const scope = effectScope()
  const recording = scope.run(() => create({ editor, currentTime: computed(() => playback.currentTime),
    pausePlayback() { calls.push(['pause']); playback.beginRequest('pause', 'pausing') },
    async requestPlayback(position) { calls.push(['play', position]); if (playGate) await playGate.promise; if (!playResult) playback.error = 'Audio output failed'; return playResult },
  }))
  t.after(async () => { await recording.cancelRecording(); unmount.forEach(action => action()); scope.stop(); playback.$dispose() })
  return { recording, playback, editor, calls, processors, streams, contexts, files,
    feed(samples) { processors.at(-1)?.onaudioprocess?.({ inputBuffer: { getChannelData: () => Float32Array.from(samples) } }) },
    advance(milliseconds) { now += milliseconds; const pending = [...frames.values()]; frames.clear(); pending.forEach(callback => callback()) },
  }
}

test('plugin recording requires the native capability before requesting microphone access', async t => {
  for (const capability of [false, null]) {
    const env = environment(t, { capability })
    assert.equal(await env.recording.startRecording(), false)
    assert.equal(env.recording.error.value, 'editor.plugins.recordingBlocked')
    assert.equal(env.calls.some(call => call[0] === 'permission'), false)
    assert.equal(env.calls.some(call => call[0] === 'pause'), false)
  }
})

test('capture starts after native preparation and records dry PCM on its own sample clock', async t => {
  const gate = deferred(), env = environment(t, { playGate: gate })
  const started = env.recording.startRecording()
  await flush()
  assert.equal(env.recording.state.value, 'preparing')
  assert.equal(env.playback.recordingProjectId, 'project')
  assert.equal(env.playback.loop, false)
  assert.deepEqual(env.playback.suppressedTrackIds, ['take'])
  env.feed([1, 1])
  env.advance(3000)
  gate.resolve()
  assert.equal(await started, true)
  env.feed([0.5, -0.5, 2, -2])
  env.advance(5000)
  assert.equal(env.recording.elapsed.value, 4 / 48000)
  const result = await env.recording.stopRecording()
  assert.equal(result.duration, 4 / 48000)
  const bytes = Buffer.concat(env.files.get('capture-1'))
  assert.deepEqual([0, 2, 4, 6].map(offset => bytes.readInt16LE(offset)), [16384, -16384, 32767, -32768])
  assert.equal(env.calls.find(call => call[0] === 'attach')[3], 2)
  assert.equal(env.playback.currentTime, 2 + 4 / 48000)
  assert.equal(env.playback.recordingProjectId, null)
  assert.equal(env.playback.loop, true)
  assert.deepEqual(env.playback.suppressedTrackIds, [])
  assert.equal(env.streams[0].track.stops, 1)
  assert.equal(env.contexts[0].state, 'closed')
  assert.equal(env.editor.session.pluginChain[0].state, 'saved')
})

test('native playback failure cancels the partial take and releases microphone resources', async t => {
  const env = environment(t, { playResult: false })
  assert.equal(await env.recording.startRecording(), false)
  assert.equal(env.recording.state.value, 'idle')
  assert.equal(env.recording.error.value, 'Audio output failed')
  assert.ok(env.calls.some(call => call[0] === 'cancel_editor_recording'))
  assert.equal(env.calls.some(call => call[0] === 'finish_editor_recording' || call[0] === 'attach'), false)
  assert.equal(env.playback.recordingProjectId, null)
  assert.equal(env.playback.loop, true)
  assert.equal(env.streams[0].track.stops, 1)
})

test('cancelling native startup prevents late playback completion from starting capture', async t => {
  const gate = deferred(), env = environment(t, { playGate: gate })
  const started = env.recording.startRecording()
  await flush()
  await env.recording.cancelRecording()
  gate.resolve()
  assert.equal(await started, false)
  env.feed([1, 1])
  assert.equal(env.recording.state.value, 'idle')
  assert.equal(env.calls.some(call => call[0] === 'append_editor_recording_chunk'), false)
  assert.equal(env.playback.recordingProjectId, null)
})

test('replacing the same project during permission or finalization never attaches to its replacement', async t => {
  const permission = deferred(), env = environment(t, { permissionGate: permission })
  const started = env.recording.startRecording()
  await flush()
  env.editor.session = { ...env.editor.session }
  await flush()
  permission.resolve()
  assert.equal(await started, false)
  assert.equal(env.calls.some(call => call[0] === 'begin_editor_recording'), false)
  assert.equal(env.streams[0].track.stops, 1)
  const finalized = deferred(), second = environment(t, { finalizeGate: finalized })
  await second.recording.startRecording()
  second.feed([0.5])
  const stopped = second.recording.stopRecording()
  await flush()
  second.editor.session = { ...second.editor.session }
  finalized.resolve()
  assert.equal(await stopped, null)
  assert.equal(second.calls.some(call => call[0] === 'attach'), false)
  assert.ok(second.calls.some(call => call[0] === 'discard_editor_recording'))
})

test('late microphone callbacks cannot write to a newer take', async t => {
  const env = environment(t)
  await env.recording.startRecording()
  const oldCallback = env.processors[0].onaudioprocess
  await env.recording.cancelRecording()
  await env.recording.startRecording()
  oldCallback({ inputBuffer: { getChannelData: () => Float32Array.from([1, 1, 1]) } })
  env.feed([0.5])
  const result = await env.recording.stopRecording()
  assert.equal(result.duration, 1 / 48000)
})

test('projects without plugins retain their existing dry-recording path', async t => {
  const env = environment(t, { plugins: false, capability: false, playResult: false })
  assert.equal(await env.recording.startRecording(), true)
  assert.equal(env.calls.some(call => call[0] === 'capability'), false)
  env.advance(500)
  assert.equal(env.recording.elapsed.value, 0.5)
  assert.equal(env.playback.loop, true)
  await env.recording.cancelRecording()
})

test('recording write errors stop capture and release native transport without waiting for user stop', async t => {
  const env = environment(t, { appendFailure: 'Recording storage failed' })
  await env.recording.startRecording()
  env.feed(new Float32Array(16384))
  await flush()
  assert.equal(env.recording.state.value, 'idle')
  assert.match(env.recording.error.value, /Recording storage failed/)
  assert.equal(env.streams[0].track.stops, 1)
  assert.equal(env.playback.recordingProjectId, null)
  assert.equal(env.calls.some(call => call[0] === 'finish_editor_recording'), false)
})

test('slow recording storage has a bounded PCM queue and stops microphone capture', async t => {
  const gate = deferred(), env = environment(t, { appendGate: gate })
  await env.recording.startRecording()
  env.feed(new Float32Array(16384))
  await flush()
  const samples = new Float32Array(4096)
  for (let count = 0; count < 1028; count++) env.feed(samples)
  assert.equal(env.recording.state.value, 'stopping')
  assert.equal(env.streams[0].track.stops, 1)
  assert.equal(env.recording.error.value, 'editor.recordingWriteBacklog')
  gate.resolve()
  await env.recording.stopRecording()
  assert.equal(env.recording.state.value, 'idle')
  assert.equal(env.playback.recordingProjectId, null)
  assert.equal(env.calls.some(call => call[0] === 'finish_editor_recording'), false)
})
