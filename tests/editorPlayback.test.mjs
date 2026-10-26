import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { computed, effectScope, reactive, ref, watch, nextTick } from 'vue'
import { createPinia, setActivePinia, storeToRefs } from 'pinia'
import { useEditorPlaybackStore } from '../src/stores/editorPlayback.ts'
import { clipIsActive, clipMediaTime, clipsForTrack } from '../src/utils/editorClips.ts'
import { EditorTimelinePlayback } from '../src/utils/editorAudio.ts'
import { normalizeMasterLimiter } from '../src/utils/masterLimiter.ts'
import { hasEditorPlugins, pluginProjectSignature } from '../src/utils/editorPlugins.ts'

const source = ts.createSourceFile('playback.ts', readFileSync(new URL('../src/composables/useEditorPlayback.ts', import.meta.url), 'utf8'), ts.ScriptTarget.Latest, true)
const code = ts.transpileModule(source.statements.filter(item => !ts.isImportDeclaration(item)).map(item => item.getText(source)).join('\n').replace('export function useEditorPlayback', 'function useEditorPlayback'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None },
}).outputText
const nativeSource = ts.createSourceFile('native.ts', readFileSync(new URL('../src/composables/useEditorNativePlayback.ts', import.meta.url), 'utf8'), ts.ScriptTarget.Latest, true)
const nativeCode = ts.transpileModule(nativeSource.statements.filter(item => !ts.isImportDeclaration(item)).map(item => item.getText(nativeSource)).join('\n').replace('export function useEditorNativePlayback', 'function useEditorNativePlayback'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None },
}).outputText
const flush = async () => { await nextTick(); await new Promise(resolve => setImmediate(resolve)) }
function deferred() {
  let resolve, reject
  const promise = new Promise((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

function environment(t, { clips = [{ start: 0, offset: 0, duration: 20 }], contextState = 'running', resumeGate, platform = '', chunksGate, metadataReadyState = 4, nativePlugins = false, nativePauseGate, limiter, workletGate, graphFailure = false } = {}) {
  let now = 0, rafId = 0, parameterWrites = 0, contextCreations = 0, chunkReads = 0
  const unmount = [], nativeCalls = []
  const frames = new Map(), audios = []
  const graphNodes = new Map()
  const worklets = [], workletModules = []
  const node = () => new Proxy({
    connections: [],
    connect(destination) { this.connections.push(destination) },
    disconnect() { this.connections = [] }, fftSize: 256,
    getByteTimeDomainData(buffer) { buffer.fill(128) },
  }, { get(target, key) {
    if (key in target) return target[key]
    let value = 0
    return (target[key] = {
      get value() { return value },
      set value(next) { parameterWrites++; value = next },
      cancelScheduledValues() {}, setValueAtTime() {}, linearRampToValueAtTime() {},
    })
  } })
  const ctx = new Proxy({
    state: contextState, sampleRate: 48000, currentTime: 0, destination: node(),
    audioWorklet: { async addModule(url) { workletModules.push(url); if (workletGate) await workletGate.promise } },
    async resume() { if (resumeGate) await resumeGate.promise; this.state = 'running' },
    async close() { this.state = 'closed' },
    createBuffer(channels, length) { return { sampleRate: 48000, length, duration: length / 48000,
      numberOfChannels: channels, getChannelData: () => new Float32Array(length) } },
  }, { get(target, key) {
    if (key in target) return target[key]
    return () => {
      if (key === 'createMediaElementSource' && graphFailure) throw new Error('Graph unavailable')
      const created = node()
      if (key === 'createBufferSource') {
        created.starts = []
        created.stopped = false
        created.start = (...args) => created.starts.push(args)
        created.stop = () => { created.stopped = true }
      }
      graphNodes.set(key, [...(graphNodes.get(key) || []), created])
      return created
    }
  } })

  class FakeAudio extends EventTarget {
    _time = 0
    duration = 60
    readyState = metadataReadyState
    paused = true
    seeking = false
    ended = false
    seekDelay = 0
    seekDeadline = 0
    seeks = []
    playCalls = 0
    stalled = false
    playError = null
    playGate = null
    error = null
    get currentTime() { return this._time }
    set currentTime(value) {
      this.seeks.push(value)
      this._time = value
      this.ended = false
      this.seeking = this.seekDelay > 0
      this.seekDeadline = now + this.seekDelay
    }
    pendingPlay = null
    play() {
      this.playCalls++
      if (this.playError) return Promise.reject(this.playError)
      if (this.ended) { this._time = 0; this.ended = false }
      this.paused = false
      if (this.playGate || this.seeking || this.readyState < 3) {
        const pending = deferred()
        this.pendingPlay = pending
        if (this.playGate) this.playGate.promise.then(() => {
          if (this.pendingPlay !== pending) return
          this.pendingPlay = null
          pending.resolve()
        }, pending.reject)
        return pending.promise
      }
      return Promise.resolve()
    }
    pause() {
      this.paused = true
      this.pendingPlay?.reject(new DOMException('Playback interrupted by pause', 'AbortError'))
      this.pendingPlay = null
    }
    load() {}
    removeAttribute() {}
    advance(ms) {
      if (this.seeking && now >= this.seekDeadline) this.seeking = false
      if (this.pendingPlay && !this.playGate && !this.seeking && this.readyState >= 3) {
        this.pendingPlay.resolve()
        this.pendingPlay = null
      }
      if (!this.paused && !this.seeking && !this.stalled && this.readyState >= 3) {
        this._time = Math.min(this.duration, this._time + ms / 1000)
        if (this._time >= this.duration) { this.ended = true; this.paused = true }
      }
    }
  }

  setActivePinia(createPinia())
  const store = useEditorPlaybackStore()
  const sources = clips.map((_, index) => ({ id: `source-${index}`, path: `/audio-${index}.wav`, channels: 2, duration: 60 }))
  const editor = reactive({
    session: { id: 'project', tracks: clips.map((clip, index) => ({
      id: `track-${index}`, sourceId: sources[index].id, volume: 1, pan: 0, muted: false, solo: false,
      clips: [{ id: `clip-${index}`, assetId: sources[index].id, volume: 1, fadeIn: 0, fadeOut: 0, muted: false, ...clip }],
    })) },
    sourceMap: new Map(sources.map(item => [item.id, item])),
    duration: Math.max(...clips.map(clip => clip.start + clip.duration)),
    masterVolume: 1, masterPan: 0,
    async releasePluginSession(project) { nativeCalls.push({ op: 'unload', projectId: project.id }) },
  })
  if (limiter) editor.session.masterLimiter = normalizeMasterLimiter(limiter)
  if (nativePlugins) editor.session.pluginChain = [{ id: 'slot', format: 'vst3', pluginId: 'gain', path: '/gain.vst3', bypassed: false }]
  let nativePlaying = false, nativePosition = 0
  const createNative = vm.runInNewContext(`${nativeCode}\nuseEditorNativePlayback`, {
    computed, watch, hasEditorPlugins, pluginProjectSignature, useEditorPlaybackStore,
    pluginHostCapabilities: async () => ({ recording: true }),
    i18n: { global: { t: key => key } },
    onBeforeUnmount: callback => unmount.push(callback),
    setTimeout: () => 1, clearTimeout() {}, ensurePluginSession: async () => {},
    async pluginHostRequest(request) {
      nativeCalls.push(request)
      if (request.op === 'play') { nativePlaying = true; nativePosition = request.position }
      if (request.op === 'pause') { if (nativePauseGate) await nativePauseGate.promise; nativePlaying = false }
      if (request.op === 'seek') nativePosition = request.position
      return { projectId: request.projectId, position: nativePosition, playing: nativePlaying, duration: editor.duration }
    },
  })
  const create = vm.runInNewContext(`${code}\nuseEditorPlayback`, {
    useEditorNativePlayback: nativePlugins ? createNative : () => ({ required: ref(false) }),
    masterLimiterWorkletUrl: '/limiter-worklet.js',
    AudioWorkletNode: function (context, name, options) {
      const created = node()
      created.port = { messages: [], postMessage(value) { this.messages.push(value) }, close() { this.closed = true } }
      created.options = options
      worklets.push(created)
      return created
    },
    setTimeout, clearTimeout,
    normalizeMasterLimiter, watch, storeToRefs, useEditorPlaybackStore, clipIsActive, clipMediaTime, clipsForTrack, EditorTimelinePlayback,
    createPreviewChunkReader: () => async () => {
      chunkReads++
      if (chunksGate) await chunksGate.promise
      return ctx.createBuffer(2, 48000 * 4)
    },
    onBeforeUnmount: callback => unmount.push(callback),
    convertFileSrc: value => value,
    createEditorAudioElement: url => { const audio = new FakeAudio(); audio.src = url; audios.push(audio); return audio },
    i18n: { global: { t: key => key } },
    window: { AudioContext: function () { contextCreations++; return ctx }, navigator: { platform } },
    performance: { now: () => now },
    requestAnimationFrame: callback => { frames.set(++rafId, callback); return rafId },
    cancelAnimationFrame: id => frames.delete(id),
  })
  const scope = effectScope()
  const playback = scope.run(() => create({ editor, scrollEl: ref(null) }))
  let disposed = false
  const dispose = () => { if (!disposed) { disposed = true; unmount.forEach(callback => callback()); scope.stop(); store.$dispose() } }
  t.after(dispose)
  function frame(ms = 16, advanceMedia = true) {
    now += ms
    if (ctx.state === 'running') ctx.currentTime += ms / 1000
    if (advanceMedia) audios.forEach(audio => audio.advance(ms))
    const pending = [...frames.values()]
    frames.clear()
    pending.forEach(callback => callback(now))
  }
  return { playback, store, editor, audios, ctx, graphNodes, frame, dispose, nativeCalls, worklets, workletModules,
    resources: () => ({ contextCreations, chunkReads }), parameterWrites: () => parameterWrites }
}

test('removing the last plugin waits for native pause and restores the Web Audio transport', async t => {
  const pauseGate = deferred()
  const env = environment(t, { nativePlugins: true, nativePauseGate: pauseGate })
  assert.equal(await env.playback.requestPlay(2), true)
  env.editor.session.pluginChain = []
  await flush()
  assert.equal(env.store.status, 'pausing')
  assert.equal(env.store.transportCanToggle, false)
  pauseGate.resolve()
  await flush()
  assert.equal(env.store.status, 'paused')
  assert.equal(env.store.transportCanToggle, true)
  assert.equal(env.store.currentTime, 2)
  assert.equal(await env.playback.requestPlay(), true)
  assert.equal(env.audios.length, 1)
  assert.ok(env.nativeCalls.some(request => request.op === 'pause'))
})

test('a stalled media clock does not advance the playhead or trigger repeated seeks', async t => {
  const env = environment(t)
  assert.equal(await env.playback.requestPlay(), true)
  env.frame(100)
  const audio = env.audios[0]
  audio.stalled = true
  audio.seekDelay = 250
  const before = audio.seeks.length
  for (let i = 0; i < 120; i++) env.frame()
  assert.equal(audio.seeks.length, before)
  assert.ok(Math.abs(env.store.currentTime - 0.1) < 0.001)
})

test('the track graph routes media through gain and balance into the master bus and steady playback retains it', async t => {
  const env = environment(t)
  await env.playback.requestPlay()
  for (const kind of ['createBiquadFilter', 'createDynamicsCompressor', 'createConvolver', 'createDelay']) {
    assert.equal(env.graphNodes.has(kind), false)
  }
  const [mediaSource] = env.graphNodes.get('createMediaElementSource')
  const [masterInput, , , trackGain] = env.graphNodes.get('createGain')
  const [masterMerger, trackMerger] = env.graphNodes.get('createChannelMerger')
  assert.ok(mediaSource.connections.includes(trackGain))
  assert.ok(trackMerger.connections.includes(masterInput))
  assert.ok(masterMerger.connections.includes(env.ctx.destination))
  const connections = [...env.graphNodes.values()].flat().map(node => node.connections)
  const writes = env.parameterWrites()
  for (let index = 0; index < 100; index++) env.frame()
  assert.deepEqual([...env.graphNodes.values()].flat().map(node => node.connections), connections)
  assert.equal(env.parameterWrites(), writes)
  assert.equal(env.audios[0].playCalls, 1)
  assert.equal(env.audios[0].seeks.length, 0)
})

test('track and master meters report analyser levels during playback', async t => {
  const env = environment(t)
  await env.playback.requestPlay()
  for (const analyser of env.graphNodes.get('createAnalyser')) {
    analyser.getByteTimeDomainData = buffer => buffer.fill(192)
  }
  env.frame()
  const [left, right] = env.store.trackLevels['track-0']
  assert.ok(left > 0 && right > 0)
  assert.ok(env.store.masterLevel[0] > 0 && env.store.masterLevel[1] > 0)
})

test('buffering one track holds the group and resumes without a wall-clock jump', async t => {
  const env = environment(t, { clips: [{ start: 0, offset: 0, duration: 20 }, { start: 0, offset: 5, duration: 20 }] })
  await env.playback.requestPlay()
  env.frame(100)
  env.audios[1].readyState = 2
  env.frame(16)
  const held = env.store.currentTime
  const seeks = env.audios.map(audio => audio.seeks.length)
  for (let i = 0; i < 80; i++) env.frame()
  assert.equal(env.store.currentTime, held)
  assert.ok(env.audios.every(audio => audio.paused))
  assert.deepEqual(env.audios.map(audio => audio.seeks.length), seeks)
  env.audios[1].readyState = 4
  env.frame()
  await flush()
  env.frame(100)
  assert.ok(env.store.currentTime > held && env.store.currentTime < held + 0.2)
})

test('an in-flight 250 ms seek is allowed to complete without repeated correction', async t => {
  const env = environment(t)
  await env.playback.requestPlay()
  env.frame(100)
  const audio = env.audios[0]
  audio.seekDelay = 250
  env.playback.seek(5)
  await flush()
  const count = audio.seeks.length
  for (let i = 0; i < 14; i++) env.frame()
  assert.equal(audio.seeks.length, count)
  assert.equal(env.store.currentTime, 5)
  for (let i = 0; i < 20; i++) { env.frame(); await flush() }
  assert.equal(audio.seeks.length, count)
  assert.ok(env.store.currentTime > 5 && env.store.currentTime < 5.5)
})

test('trimmed clips, gaps and late frames preserve clip start boundaries', async t => {
  const env = environment(t, { clips: [{ start: 0, offset: 3, duration: 1 }, { start: 2, offset: 7, duration: 1 }] })
  await env.playback.requestPlay()
  env.frame(500)
  assert.ok(Math.abs(env.store.currentTime - 0.5) < 0.001)
  env.frame(500)
  assert.ok(env.audios[0].paused)
  env.frame(1500)
  assert.equal(env.store.currentTime, 2)
  assert.equal(env.audios[1].currentTime, 7)
  await flush()
  env.frame(500)
  assert.ok(Math.abs(env.store.currentTime - 2.5) < 0.001)
  env.frame(500)
  assert.equal(env.store.status, 'paused')
  assert.equal(env.store.currentTime, 0)
})

test('seek startup latency is not counted as played audio', async t => {
  const env = environment(t)
  await env.playback.requestPlay()
  const gate = deferred()
  env.audios[0].playGate = gate
  env.playback.seek(8)
  env.frame(1200, false)
  gate.resolve()
  await flush()
  env.frame(100)
  assert.ok(Math.abs(env.store.currentTime - 8.1) < 0.001)
})

for (const action of ['pause', 'seek', 'dispose']) {
  test(`a ${action} during context resume invalidates the old play request`, async t => {
    const gate = deferred()
    const env = environment(t, { contextState: 'suspended', resumeGate: gate })
    const pending = env.playback.requestPlay()
    if (action === 'pause') env.playback.requestPause()
    if (action === 'seek') { env.playback.seek(4); env.playback.requestPause() }
    if (action === 'dispose') env.dispose()
    gate.resolve()
    assert.equal(await pending, false)
    await flush()
    assert.ok(env.audios.every(audio => audio.paused && audio.playCalls === 0))
    assert.equal(env.store.currentTime, action === 'seek' ? 4 : 0)
  })
}

test('context resume failure is reported without starting silent media', async t => {
  const gate = deferred()
  const env = environment(t, { contextState: 'suspended', resumeGate: gate })
  const pending = env.playback.requestPlay()
  gate.reject(new Error('Audio output unavailable'))
  assert.equal(await pending, false)
  assert.equal(env.store.status, 'error')
  assert.match(env.store.error, /Audio output unavailable/)
  assert.ok(env.audios.every(audio => audio.paused))
})

test('one rejected track stops the whole mix and retains the failure', async t => {
  const env = environment(t, { clips: [{ start: 0, offset: 0, duration: 20 }, { start: 0, offset: 0, duration: 20 }] })
  env.audios[1].playError = new Error('Unsupported audio')
  assert.equal(await env.playback.requestPlay(), false)
  assert.equal(env.store.status, 'error')
  assert.match(env.store.error, /Unsupported audio/)
  assert.ok(env.audios.every(audio => audio.paused))
})

test('loop restarts at the requested media offsets', async t => {
  const env = environment(t, { clips: [{ start: 0, offset: 4, duration: 1 }] })
  env.store.setLoop(true)
  await env.playback.requestPlay()
  env.frame(1000)
  await flush()
  assert.equal(env.store.status, 'playing')
  assert.equal(env.store.currentTime, 0)
  assert.equal(env.audios[0].currentTime, 4)
  env.frame(100)
  assert.ok(Math.abs(env.store.currentTime - 0.1) < 0.001)
})

test('a media decode error during playback stops every track', async t => {
  const env = environment(t, { clips: [{ start: 0, offset: 0, duration: 20 }, { start: 0, offset: 0, duration: 20 }] })
  await env.playback.requestPlay()
  env.frame(100)
  env.audios[1].error = { code: 3, message: 'Audio decoding failed' }
  env.audios[1].dispatchEvent(new Event('error'))
  assert.equal(env.store.status, 'error')
  assert.match(env.store.error, /Audio decoding failed/)
  assert.ok(env.audios.every(audio => audio.paused))
})

test('failure to start a later clip is reported instead of retrying each frame', async t => {
  const env = environment(t, { clips: [{ start: 0, offset: 0, duration: 2 }, { start: 1, offset: 0, duration: 2 }] })
  env.audios[1].playError = new Error('Output unavailable')
  await env.playback.requestPlay()
  env.frame(1000)
  await flush()
  assert.equal(env.store.status, 'error')
  for (let i = 0; i < 20; i++) env.frame()
  assert.equal(env.audios[1].playCalls, 1)
  assert.ok(env.audios.every(audio => audio.paused))
})

test('interrupted audio output holds both the timeline and media until output returns', async t => {
  const env = environment(t)
  await env.playback.requestPlay()
  env.frame(100)
  env.ctx.state = 'interrupted'
  for (let i = 0; i < 100; i++) env.frame()
  assert.equal(env.store.currentTime, 0.1)
  assert.ok(env.audios[0].paused)
  env.ctx.state = 'running'
  env.frame()
  await flush()
  env.frame(100)
  assert.ok(env.store.currentTime > 0.1 && env.store.currentTime < 0.3)
})

test('mute and solo changes retain the actual playback position', async t => {
  const env = environment(t, { clips: [{ start: 0, offset: 0, duration: 20 }, { start: 0, offset: 3, duration: 20 }] })
  await env.playback.requestPlay()
  env.frame(100)
  env.editor.session.tracks[1].solo = true
  await flush()
  assert.ok(env.audios[0].paused)
  env.frame(100)
  assert.ok(Math.abs(env.store.currentTime - 0.2) < 0.001)
  env.editor.session.tracks[1].solo = false
  env.editor.session.tracks[1].muted = true
  await flush()
  env.frame(100)
  assert.ok(Math.abs(env.store.currentTime - 0.3) < 0.001)
  assert.ok(env.audios[1].paused)
})

test('a track that ends slightly ahead of the mix does not restart from the beginning', async t => {
  const env = environment(t, { clips: [{ start: 0, offset: 0, duration: 1 }, { start: 0, offset: 0, duration: 2 }] })
  env.audios[0].duration = 1
  await env.playback.requestPlay()
  env.frame(900)
  env.audios[1].stalled = true
  env.frame(100)
  assert.equal(env.audios[0].ended, true)
  assert.equal(env.audios[0].playCalls, 1)
  env.audios[1].stalled = false
  env.frame(200)
  assert.ok(env.store.currentTime >= 1)
  assert.ok(env.audios[0].paused)
})

test('a late error from a replaced audio entry cannot stop the current source', async t => {
  const env = environment(t)
  await env.playback.requestPlay()
  const previous = env.audios[0]
  env.editor.sourceMap.set('replacement', { id: 'replacement', path: '/new.wav', channels: 2, duration: 60 })
  env.editor.session.tracks[0].clips[0].assetId = 'replacement'
  await flush()
  assert.equal(env.store.status, 'playing')
  previous.error = { code: 3, message: 'Old decoder closed' }
  previous.dispatchEvent(new Event('error'))
  assert.equal(env.store.status, 'playing')
  assert.equal(env.store.error, null)
  assert.equal(env.audios[1].paused, false)
})

test('master gain changes during pending startup do not cancel the play request', async t => {
  const env = environment(t)
  const gate = deferred()
  env.audios[0].playGate = gate
  const starting = env.playback.requestPlay()
  env.editor.masterVolume = 0.4
  await flush()
  gate.resolve()
  assert.equal(await starting, true)
  assert.equal(env.store.status, 'playing')
})

test('muting every track can be recovered by unmuting and starting playback', async t => {
  const env = environment(t)
  await env.playback.requestPlay()
  const previous = env.audios[0]
  env.editor.session.tracks[0].muted = true
  await flush()
  assert.ok(previous.paused)
  env.editor.session.tracks[0].muted = false
  await flush()
  assert.equal(await env.playback.requestPlay(), true)
  assert.equal(env.store.status, 'playing')
})

for (const action of ['pause', 'seek', 'dispose', 'project']) {
  test(`a ${action} cancels pending media playback without affecting the latest request`, async t => {
    const env = environment(t)
    const gate = deferred()
    env.audios[0].playGate = gate
    const starting = env.playback.requestPlay()
    if (action === 'pause') env.playback.requestPause()
    if (action === 'seek') env.playback.seek(6)
    if (action === 'dispose') env.dispose()
    if (action === 'project') { env.editor.session.id = 'next-project'; await flush() }
    assert.equal(await starting, false)
    gate.resolve()
    await flush()
    assert.equal(env.store.error, null)
    assert.equal(env.store.status, action === 'seek' ? 'playing' : 'paused')
    assert.equal(env.store.currentTime, action === 'seek' ? 6 : 0)
    assert.equal(env.audios[0].paused, action !== 'seek')
  })
}

test('rapid seeks keep the newest target and a completed source can be replayed explicitly', async t => {
  const env = environment(t, { clips: [{ start: 0, offset: 0, duration: 1 }, { start: 0, offset: 0, duration: 3 }] })
  env.audios[0].duration = 1
  await env.playback.requestPlay()
  env.frame(1200)
  assert.ok(env.audios[0].ended)
  env.audios.forEach(audio => { audio.seekDelay = 250 })
  env.playback.seek(0.1)
  env.playback.seek(0.7)
  env.playback.seek(0.3)
  env.frame(250, true)
  await flush()
  assert.equal(env.store.status, 'playing')
  assert.equal(env.store.error, null)
  assert.equal(env.store.currentTime, 0.3)
  assert.ok(env.audios.every(audio => !audio.paused))
  env.frame(100)
  assert.ok(env.store.currentTime > 0.3 && env.store.currentTime < 0.8)
})

test('a decoder error during startup retains its cause after pending plays are cancelled', async t => {
  const env = environment(t)
  env.audios[0].playGate = deferred()
  const starting = env.playback.requestPlay()
  env.audios[0].error = { code: 3, message: 'Decoder failed to open audio' }
  env.audios[0].dispatchEvent(new Event('error'))
  assert.equal(await starting, false)
  assert.equal(env.store.status, 'error')
  assert.match(env.store.error, /Decoder failed to open audio/)
  assert.ok(env.audios.every(audio => audio.paused))
})

test('a media failure invalidates startup still waiting for the audio context', async t => {
  const gate = deferred()
  const env = environment(t, { contextState: 'suspended', resumeGate: gate })
  const starting = env.playback.requestPlay()
  env.audios[0].error = { code: 3, message: 'Decoder failed to open audio' }
  env.audios[0].dispatchEvent(new Event('error'))
  gate.resolve()
  assert.equal(await starting, false)
  assert.equal(env.store.status, 'error')
  assert.equal(env.audios[0].playCalls, 0)
  assert.ok(env.audios[0].paused)
})

test('track gain and pan changes update the graph without restarting playback', async t => {
  const env = environment(t)
  await env.playback.requestPlay()
  env.frame(100)
  const audio = env.audios[0]
  const before = { seeks: audio.seeks.length, plays: audio.playCalls }
  const track = env.editor.session.tracks[0]
  track.volume = 0.37
  track.pan = 0.25
  await flush()
  assert.equal(audio.seeks.length, before.seeks)
  assert.equal(audio.playCalls, before.plays)
  assert.equal(env.store.status, 'playing')
  assert.ok(env.graphNodes.get('createGain').some(gain => gain.gain.value === 0.37))
  assert.ok(env.graphNodes.get('createGain').some(gain => gain.gain.value === 0.75))
})

test('relinking the only offline source replaces the stale media URL', async t => {
  const env = environment(t)
  await env.playback.requestPlay()
  const source = env.editor.sourceMap.get('source-0')
  source.missing = true
  await flush()
  source.path = '/relinked.wav'
  source.missing = false
  await flush()
  assert.equal(await env.playback.requestPlay(), true)
  const playing = env.audios.filter(audio => !audio.paused)
  assert.equal(playing.length, 1)
  assert.equal(playing[0].src, '/relinked.wav')
})

test('clip offset edits still seek to the new media position during playback', async t => {
  const env = environment(t)
  await env.playback.requestPlay()
  env.frame(100)
  env.editor.session.tracks[0].clips[0].offset = 2
  await flush()
  assert.equal(env.store.status, 'playing')
  assert.equal(env.audios[0].currentTime, 2.1)
  env.frame(100)
  assert.ok(Math.abs(env.store.currentTime - 0.2) < 0.001)
})

test('clip gain and fade edits update output without cancelling pending startup', async t => {
  const env = environment(t)
  const gate = deferred()
  env.audios[0].playGate = gate
  const starting = env.playback.requestPlay(0.5)
  const clip = env.editor.session.tracks[0].clips[0]
  clip.volume = 0.7
  clip.fadeIn = 1
  await flush()
  assert.ok(env.graphNodes.get('createGain').some(gain => gain.gain.value === 0.35))
  gate.resolve()
  assert.equal(await starting, true)
  assert.equal(env.audios[0].playCalls, 1)
  assert.equal(env.store.status, 'playing')
})

test('recording suppression excludes the recording track and preserves other playback', async t => {
  const env = environment(t, { clips: [{ start: 0, offset: 0, duration: 20 }, { start: 0, offset: 0, duration: 20 }] })
  env.store.setSuppressedTrackIds(['track-1'])
  await flush()
  assert.equal(await env.playback.requestPlay(), true)
  assert.equal(env.audios[1].playCalls, 0)
  env.frame(500)
  assert.equal(env.store.currentTime, 0.5)
  env.playback.requestPause()
  env.store.setSuppressedTrackIds([])
  await flush()
  assert.equal(env.store.status, 'paused')
  assert.ok(env.audios.every(audio => audio.paused))
})

test('an unexpected AbortError when starting a later clip stops the mix without repeated retries', async t => {
  const env = environment(t, { clips: [{ start: 0, offset: 0, duration: 3 }, { start: 1, offset: 0, duration: 2 }] })
  env.audios[1].playError = new DOMException('The media resource was interrupted', 'AbortError')
  await env.playback.requestPlay()
  env.frame(1000)
  await flush()
  assert.equal(env.store.status, 'error')
  assert.match(env.store.error, /media resource was interrupted/)
  for (let index = 0; index < 10; index++) { env.frame(); await flush() }
  assert.equal(env.audios[1].playCalls, 1)
  assert.ok(env.audios.every(audio => audio.paused))
})

test('buffering intentionally cancels a pending clip start without failing the mix', async t => {
  const env = environment(t, { clips: [{ start: 0, offset: 0, duration: 3 }, { start: 1, offset: 0, duration: 2 }] })
  const gate = deferred()
  env.audios[1].playGate = gate
  await env.playback.requestPlay()
  env.frame(1000)
  env.audios[1].readyState = 2
  env.frame()
  await flush()
  assert.equal(env.store.status, 'playing')
  assert.equal(env.store.error, null)
  assert.ok(env.audios.every(audio => audio.paused))
  env.audios[1].readyState = 4
  gate.resolve()
  env.frame()
  await flush()
  env.frame(100)
  assert.equal(env.store.status, 'playing')
  assert.ok(env.store.currentTime > 1)
  assert.ok(env.audios.every(audio => !audio.paused))
})


for (const platform of ['MacIntel', 'Win32', 'Linux x86_64']) {
test(`${platform} transport schedules later clips with one clock instead of separate media players`, async t => {
  const env = environment(t, { platform, clips: [
    { start: 0, offset: 0, duration: 20 }, { start: 2, offset: 4, duration: 10 },
  ] })
  assert.equal(await env.playback.requestPlay(), true)
  assert.equal(env.audios.length, 0)
  const starts = env.graphNodes.get('createBufferSource').map(node => node.starts[0])
  assert.ok(starts.some(([when, offset]) => Math.abs(when - 2.06) < 1e-8 && offset === 0))
  // A delayed UI frame must not start the second clip early or move its start.
  env.frame(2500)
  assert.ok(Math.abs(env.store.currentTime - 2.44) < 1e-8)
  assert.equal(env.store.status, 'playing')
  env.playback.requestPause()
  assert.ok(env.graphNodes.get('createBufferSource').every(node => node.stopped))
  assert.equal(env.store.currentTime, 2.44)
})
}

test('Windows rapid seeks stop the old output and resume only the latest decoded position', async t => {
  const gate = deferred()
  const env = environment(t, { platform: 'Win32', chunksGate: gate })
  const starting = env.playback.requestPlay()
  await flush()
  env.playback.seek(5)
  env.playback.seek(9)
  env.playback.seek(1.2)
  env.frame(2000)
  assert.equal(env.store.currentTime, 1.2)
  gate.resolve()
  assert.equal(await starting, false)
  await flush()
  assert.equal(env.audios.length, 0)
  assert.equal(env.store.status, 'playing')
  const nodes = env.graphNodes.get('createBufferSource')
  assert.ok(nodes.length > 0)
  assert.equal(nodes[0].starts[0][1], 1.2)
  assert.ok(nodes.every(node => node.starts[0][0] >= 2.06))
  env.frame(100)
  assert.ok(Math.abs(env.store.currentTime - 1.24) < 1e-8)
})

test('Windows cached seeks replace scheduled audio without decoding or accelerating the clock', async t => {
  const env = environment(t, { platform: 'Win32' })
  assert.equal(await env.playback.requestPlay(0.1), true)
  await flush()
  const reads = env.resources().chunkReads
  const previous = [...env.graphNodes.get('createBufferSource')]
  for (const time of [0.2, 0.7, 0.3]) env.playback.seek(time)
  await flush()
  assert.ok(previous.every(node => node.stopped))
  assert.equal(env.resources().chunkReads, reads)
  assert.equal(env.audios.length, 0)
  assert.equal(env.store.status, 'playing')
  env.frame(260)
  assert.ok(Math.abs(env.store.currentTime - 0.5) < 1e-8)
  env.frame(200)
  assert.ok(Math.abs(env.store.currentTime - 0.7) < 1e-8)
})

test('a macOS pause cancels startup waiting for decoded chunks immediately', async t => {
  const gate = deferred()
  const env = environment(t, { platform: 'MacIntel', chunksGate: gate })
  const playing = env.playback.requestPlay()
  await flush()
  env.playback.requestPause()
  assert.equal(await playing, false)
  gate.resolve()
  await flush()
  assert.equal(env.store.status, 'paused')
  assert.equal(env.graphNodes.get('createBufferSource')?.length || 0, 0)
})

test('background sample-rate hydration replaces an in-flight macOS start', async t => {
  const gate = deferred()
  const env = environment(t, { platform: 'MacIntel', chunksGate: gate })
  const starting = env.playback.requestPlay()
  await flush()
  const request = env.store.requestId
  env.editor.sourceMap.get('source-0').sampleRate = 44100
  await flush()
  assert.ok(env.store.requestId > request)
  assert.equal(await starting, false)
  gate.resolve()
  await flush()
  assert.equal(env.store.status, 'playing')
  assert.equal(env.store.error, null)
})

test('sample-rate hydration does not restart the fallback media transport', async t => {
  const env = environment(t)
  assert.equal(await env.playback.requestPlay(), true)
  env.frame(100)
  const request = env.store.requestId
  env.editor.sourceMap.get('source-0').sampleRate = 44100
  await flush()
  assert.equal(env.store.requestId, request)
  assert.equal(env.audios[0].playCalls, 1)
  assert.equal(env.audios[0].seeks.length, 0)
  env.frame(100)
  assert.equal(env.store.currentTime, 0.2)
})

test('a rejected macOS play request stops existing audio and retains its error', async t => {
  const env = environment(t, { platform: 'MacIntel' })
  assert.equal(await env.playback.requestPlay(), true)
  env.frame(100)
  const nodes = env.graphNodes.get('createBufferSource')
  env.editor.sourceMap.get('source-0').missing = true
  assert.equal(await env.playback.requestPlay(), false)
  await flush()
  assert.equal(env.store.status, 'error')
  assert.match(env.store.error, /assetOfflineBlocked/)
  assert.ok(nodes.every(node => node.stopped))
})

for (const looping of [false, true]) {
  test(`macOS playback ${looping ? 'loops' : 'finishes'} when animation frames stop`, async t => {
    const env = environment(t, { platform: 'MacIntel', clips: [{ start: 0, offset: 0, duration: 1 }] })
    env.store.setLoop(looping)
    assert.equal(await env.playback.requestPlay(), true)
    env.ctx.currentTime = 2
    // The audio timer runs, while no UI frame is dispatched.
    await new Promise(resolve => setTimeout(resolve, 150))
    assert.equal(env.store.status, looping ? 'playing' : 'paused')
    assert.equal(env.store.currentTime, 0)
    const sources = env.graphNodes.get('createBufferSource')
    if (looping) assert.ok(sources.some(node => node.starts[0][0] >= 2.06 && !node.stopped))
    else assert.ok(sources.every(node => node.stopped))
  })
}

test('unmounting before macOS warmup resumes prevents recreation of audio resources', async t => {
  const env = environment(t, { platform: 'MacIntel' })
  env.dispose()
  const resources = env.resources()
  await flush()
  assert.equal(env.ctx.state, 'closed')
  assert.deepEqual(env.resources(), resources)
  assert.equal(env.resources().chunkReads, 0)
  // A retained warmup callback must also remain inert after unmount.
  await env.playback.preloadActiveTracks()
  assert.deepEqual(env.resources(), resources)
})

test('late fallback media metadata does not recreate the audio context after unmount', async t => {
  const env = environment(t, { metadataReadyState: 0 })
  env.dispose()
  const resources = env.resources()
  env.audios[0].dispatchEvent(new Event('loadedmetadata'))
  await flush()
  assert.equal(env.ctx.state, 'closed')
  assert.deepEqual(env.resources(), resources)
})

test('pending native warmup decoding remains cancelled after unmount', async t => {
  const gate = deferred()
  const env = environment(t, { platform: 'MacIntel', chunksGate: gate })
  await flush()
  assert.ok(env.resources().chunkReads > 0)
  env.dispose()
  const resources = env.resources()
  gate.resolve()
  await flush()
  assert.deepEqual(env.resources(), resources)
  assert.equal(env.graphNodes.get('createBufferSource')?.length || 0, 0)
})

test('metadata completion from a previous project cannot start a new project warmup', async t => {
  const env = environment(t, { metadataReadyState: 0 })
  const audio = env.audios[0]
  env.editor.session.id = 'replacement-project'
  await flush()
  const pending = env.playback.preloadActiveTracks()
  const resources = env.resources()
  env.editor.session = null
  await flush()
  audio.dispatchEvent(new Event('loadedmetadata'))
  env.audios.forEach(audio => audio.dispatchEvent(new Event('loadedmetadata')))
  await pending
  await flush()
  assert.deepEqual(env.resources(), resources)
})

test('master limiter setup waits before playback, uses the latest config and bypass preserves its node', async t => {
  const gate = deferred()
  const env = environment(t, { limiter: { enabled: true }, workletGate: gate })
  const starting = env.playback.requestPlay()
  await flush()
  assert.equal(env.workletModules.length, 1)
  assert.equal(env.audios[0].playCalls, 0)
  env.editor.session.masterLimiter.gainDb = 12
  await flush()
  gate.resolve()
  assert.equal(await starting, true)
  assert.equal(env.worklets.length, 1)
  assert.equal(env.worklets[0].options.processorOptions.gainDb, 12)
  const node = env.worklets[0]
  assert.equal(node.connections.length, 2, 'output feeds both final meters and speakers')
  env.editor.session.masterLimiter.enabled = false
  await flush()
  assert.equal(node.port.messages.at(-1).config.enabled, false)
  env.editor.session.masterLimiter.enabled = true
  await flush()
  assert.equal(env.worklets.length, 1)
  env.playback.requestPause()
  assert.equal(node.port.messages.at(-1).type, 'reset')
  env.dispose()
  assert.equal(node.port.closed, true)
  assert.equal(node.connections.length, 0)
})

test('disabling a pending limiter ignores its late load failure and keeps dry playback running', async t => {
  const gate = deferred()
  const env = environment(t, { workletGate: gate })
  assert.equal(await env.playback.requestPlay(), true)
  env.editor.session.masterLimiter = normalizeMasterLimiter({ enabled: true })
  await flush()
  assert.equal(env.workletModules.length, 1)
  env.editor.session.masterLimiter.enabled = false
  await flush()
  gate.reject(new Error('Stale module load failed'))
  await flush()
  assert.equal(env.store.status, 'playing')
  assert.equal(env.store.error, null)
  assert.equal(env.worklets.length, 0)
  assert.ok(env.audios.every(audio => !audio.paused))
  env.ctx.audioWorklet.addModule = async () => {}
  env.editor.session.masterLimiter.enabled = true
  await flush()
  assert.equal(env.worklets.length, 1, 're-enabling after the discarded failure retries setup')
  assert.equal(env.store.status, 'playing')
})

test('disabling a limiter during startup allows dry playback when its old load fails', async t => {
  const gate = deferred()
  const env = environment(t, { limiter: { enabled: true }, workletGate: gate })
  const starting = env.playback.requestPlay()
  await flush()
  env.editor.session.masterLimiter.enabled = false
  await flush()
  gate.reject(new Error('Stale startup module load failed'))
  assert.equal(await starting, true)
  assert.equal(env.store.status, 'playing')
  assert.equal(env.store.error, null)
  assert.equal(env.worklets.length, 0)
})

test('a pending limiter load failure still stops playback if the effect remains enabled', async t => {
  const gate = deferred()
  const env = environment(t, { workletGate: gate })
  await env.playback.requestPlay()
  env.editor.session.masterLimiter = normalizeMasterLimiter({ enabled: true })
  await flush()
  gate.reject(new Error('Current module load failed'))
  await flush()
  assert.equal(env.store.status, 'error')
  assert.match(env.store.error, /Current module load failed/)
  assert.ok(env.audios.every(audio => audio.paused))
})

test('a disabled limiter processor failure restores dry routing without interrupting playback', async t => {
  const env = environment(t, { limiter: { enabled: true } })
  assert.equal(await env.playback.requestPlay(), true)
  const failedNode = env.worklets[0]
  const [masterMerger] = env.graphNodes.get('createChannelMerger')
  const [masterMeterSplitter] = env.graphNodes.get('createChannelSplitter').slice(1)
  env.editor.session.masterLimiter.enabled = false
  await flush()
  const request = env.store.requestId
  failedNode.onprocessorerror()
  assert.equal(env.store.status, 'playing')
  assert.equal(env.store.error, null)
  assert.equal(env.store.requestId, request)
  assert.ok(env.audios.every(audio => !audio.paused))
  assert.equal(failedNode.port.closed, true)
  assert.equal(failedNode.connections.length, 0)
  assert.ok(masterMerger.connections.includes(env.ctx.destination))
  assert.ok(masterMerger.connections.includes(masterMeterSplitter))
  assert.ok(!masterMerger.connections.includes(failedNode))
  env.editor.session.masterLimiter.enabled = true
  await flush()
  assert.equal(env.worklets.length, 2, 're-enabling creates a replacement processor')
  assert.equal(env.store.status, 'playing')
})

test('a retired Web Audio limiter processor cannot invalidate native playback', async t => {
  const env = environment(t, { nativePlugins: true, limiter: { enabled: true } })
  const plugin = env.editor.session.pluginChain[0]
  env.editor.session.pluginChain = []
  await flush()
  assert.equal(await env.playback.requestPlay(), true)
  const failedNode = env.worklets[0]
  env.editor.session.pluginChain = [plugin]
  await flush()
  assert.equal(await env.playback.requestPlay(2), true)
  const request = env.store.requestId
  const calls = env.nativeCalls.length
  failedNode.onprocessorerror()
  assert.equal(env.store.requestId, request)
  assert.equal(env.store.status, 'playing')
  assert.equal(env.store.error, null)
  assert.equal(env.nativeCalls.length, calls)
  assert.equal(failedNode.port.closed, true)
  assert.equal(failedNode.connections.length, 0)
})

test('an enabled current limiter processor failure stops playback and releases the failed node', async t => {
  const env = environment(t, { limiter: { enabled: true } })
  await env.playback.requestPlay()
  const failedNode = env.worklets[0]
  failedNode.onprocessorerror()
  assert.equal(env.store.status, 'error')
  assert.match(env.store.error, /limiter.failed/)
  assert.ok(env.audios.every(audio => audio.paused))
  assert.equal(failedNode.port.closed, true)
  assert.equal(failedNode.connections.length, 0)
})

test('late worklet creation is cancelled on unmount and module failure can be retried', async t => {
  const gate = deferred()
  const env = environment(t, { limiter: { enabled: true }, workletGate: gate })
  const pending = env.playback.requestPlay()
  await flush()
  env.dispose()
  gate.resolve()
  assert.equal(await pending, false)
  assert.equal(env.worklets.length, 0)
  const retry = environment(t, { limiter: { enabled: true } })
  let attempts = 0
  retry.ctx.audioWorklet.addModule = async () => { if (++attempts === 1) throw new Error('Module load failed') }
  assert.equal(await retry.playback.requestPlay(), false)
  assert.match(retry.store.error, /Module load failed/)
  assert.equal(await retry.playback.requestPlay(), true)
  retry.worklets[0].onprocessorerror()
  assert.equal(retry.store.status, 'error')
  retry.editor.session.masterLimiter.enabled = false
  await flush()
  assert.equal(await retry.playback.requestPlay(), true, 'bypass remains audible after a processor failure')
})

test('an enabled limiter rejects unconnected media and live enabling stops a dry fallback', async t => {
  const env = environment(t, { limiter: { enabled: true }, graphFailure: true })
  assert.equal(await env.playback.requestPlay(), false)
  assert.equal(env.audios[0].playCalls, 0)
  const live = environment(t, { graphFailure: true })
  assert.equal(await live.playback.requestPlay(), true)
  live.editor.session.masterLimiter = normalizeMasterLimiter({ enabled: true })
  await flush()
  assert.equal(live.store.status, 'error')
  assert.ok(live.audios.every(audio => audio.paused))
})

test('media playback reaches the final source frame then drains limiter lookahead', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const env = environment(t, { clips: [{ start: 0, offset: 0, duration: 1 }], limiter: { enabled: true, attackMs: 100 } })
  assert.equal(await env.playback.requestPlay(), true)
  env.frame(970)
  assert.equal(env.store.status, 'playing', 'do not stop 40 ms before the source end')
  env.frame(30)
  assert.equal(env.store.status, 'playing', 'keep processing the buffered final frames')
  t.mock.timers.tick(99)
  assert.equal(env.store.status, 'playing')
  t.mock.timers.tick(1)
  assert.equal(env.store.status, 'paused')
  assert.equal(env.worklets[0].port.messages.at(-1).type, 'reset')
})
