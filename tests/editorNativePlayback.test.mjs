import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { computed, effectScope, reactive, nextTick, watch } from 'vue'
import { createPinia } from 'pinia'
import { useEditorPlaybackStore } from '../src/stores/editorPlayback.ts'
import { hasEditorPlugins, pluginProjectSignature } from '../src/utils/editorPlugins.ts'
const source = ts.createSourceFile('native.ts', readFileSync(new URL('../src/composables/useEditorNativePlayback.ts', import.meta.url), 'utf8'), ts.ScriptTarget.Latest, true)
const code = ts.transpileModule(source.statements.filter(item => !ts.isImportDeclaration(item)).map(item => item.getText(source)).join('\n').replace('export function useEditorNativePlayback', 'function useEditorNativePlayback'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None },
}).outputText
const flush = async () => { await nextTick(); await new Promise(resolve => setImmediate(resolve)) }
function deferred() { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }
class PluginHostDialogPendingError extends Error {}
function environment(t, { ensureGate, statusGate, pauseGate, monitorGate, hostCapabilities = { recording: true } } = {}) {
  const playback = useEditorPlaybackStore(createPinia())
  const editor = reactive({ session: { id: 'project', tracks: [], sources: [], masterVolume: 1, masterPan: 0,
    pluginChain: [{ id: 'slot', format: 'vst3', pluginId: 'gain', name: 'Gain', path: 'gain.vst3', bypassed: false }] } })
  editor.releasePluginSession = async project => { calls.push(['unload', project.id]) }
  const calls = [], timers = new Map(), cleanup = []
  let timerId = 0
  const backend = { position: 0, playing: false, playEnds: false, error: undefined }
  const create = vm.runInNewContext(`${code}\nuseEditorNativePlayback`, {
    PluginHostDialogPendingError,
    computed, watch, hasEditorPlugins, pluginProjectSignature, useEditorPlaybackStore: () => playback,
    pluginHostCapabilities: async () => hostCapabilities,
    i18n: { global: { t: key => key } },
    onBeforeUnmount: action => cleanup.push(action),
    setTimeout: action => { timers.set(++timerId, action); return timerId }, clearTimeout: id => timers.delete(id),
    ensurePluginSession: async project => {
      calls.push(['ensure', project.id])
      const failure = backend.ensureError
      if (ensureGate) await ensureGate.promise
      if (backend.ensureGate) await backend.ensureGate.promise
      if (failure) throw new Error(failure)
    },
    snapshotPluginStates: async project => { calls.push(['snapshot', project.id]); return false },
    unloadPluginSession: async projectId => { calls.push(['unload', projectId]) },
    pluginHostRequest: async request => {
      calls.push([request.op, request.projectId, request])
      if (backend.dialogPending) throw new PluginHostDialogPendingError('Waiting for a plugin dialog')
      if (request.op === 'play') { backend.playing = !backend.playEnds; backend.position = backend.playEnds ? 10 : request.position }
      if (request.op === 'pause') { if (pauseGate) await pauseGate.promise; backend.playing = false }
      if (request.op === 'pause' && backend.pauseError) throw new Error(backend.pauseError)
      if (request.op === 'seek') backend.position = request.position
      if (request.op === 'monitor' && monitorGate) await monitorGate.promise
      if (request.op === 'status' && statusGate) return statusGate.promise
      return { projectId: request.projectId, position: backend.position, playing: backend.playing, duration: 10, masterLevel: [0.5, 0.25], error: backend.error }
    },
  })
  const scope = effectScope()
  const native = scope.run(() => create(editor, () => {}))
  t.after(async () => { cleanup.forEach(action => action()); scope.stop(); await flush(); playback.$dispose() })
  return { native, editor, playback, calls, timers, backend, async poll() { const pending = [...timers.values()]; timers.clear(); await Promise.all(pending.map(action => action())) } }
}

test('native transport sends control messages and applies host meters without PCM', async t => {
  const env = environment(t)
  assert.equal(await env.native.play(2), true)
  assert.equal(env.playback.currentTime, 2)
  assert.deepEqual(env.playback.masterLevel, [0.5, 0.25])
  env.native.seek(4)
  await flush()
  assert.equal(env.playback.currentTime, 4)
  env.native.pause(true)
  await flush()
  assert.equal(env.playback.status, 'paused')
  assert.equal(env.playback.currentTime, 0)
  assert.ok(env.calls.some(call => call[0] === 'play' && call[2].position === 2))
  assert.ok(env.calls.every(call => !call[2] || !('pcm' in call[2])))
})

test('removing a failed insert clears its playback error and resumes status polling after recovery', async t => {
  const env = environment(t)
  env.editor.session.pluginChain.push({ ...env.editor.session.pluginChain[0], id: 'good' })
  await env.native.play(2)
  env.backend.error = 'Bad plugin does not support this channel layout'
  env.backend.playing = false
  await env.poll()
  assert.equal(env.playback.status, 'error')
  assert.equal(env.timers.size, 0)
  env.backend.error = undefined
  env.editor.session.pluginChain.splice(0, 1)
  await flush()
  assert.equal(env.playback.error, null)
  assert.equal(env.playback.status, 'paused')
  assert.equal(env.timers.size, 1)
})

test('a failed initial load recovers after the bad insert is removed while valid inserts remain', async t => {
  const env = environment(t)
  env.editor.session.pluginChain.push({ ...env.editor.session.pluginChain[0], id: 'good' })
  env.backend.ensureError = 'Bad plugin load failed'
  assert.equal(await env.native.play(1), false)
  assert.match(env.playback.error, /Bad plugin load failed/)
  env.backend.ensureError = undefined
  env.editor.session.pluginChain.splice(0, 1)
  await flush()
  assert.equal(env.playback.error, null)
  assert.equal(env.playback.status, 'paused')
  assert.equal(env.timers.size, 1)
})

test('removing the final insert clears the obsolete native playback error', async t => {
  const env = environment(t)
  env.backend.ensureError = 'Bad plugin load failed'
  await env.native.play(1)
  env.editor.session.pluginChain = []
  await flush()
  assert.equal(env.native.required.value, false)
  assert.equal(env.playback.error, null)
})

test('a retiring graph cannot restore its old error after the final insert is removed', async t => {
  for (const pauseError of [undefined, 'Old graph is unavailable']) {
    const env = environment(t)
    await env.native.play(1)
    env.backend.error = 'Bad plugin processing failed'
    await env.poll()
    env.backend.pauseError = pauseError
    env.editor.session.pluginChain = []
    await flush()
    assert.equal(env.playback.status, 'paused')
    assert.equal(env.playback.error, null)
    assert.equal(env.timers.size, 0)
  }
})

test('queued healthy status from a retained graph cannot hide a failed chain update', async t => {
  const statusGate = deferred()
  const env = environment(t, { statusGate })
  await env.native.play(1)
  const updateGate = deferred()
  env.backend.ensureGate = updateGate
  env.backend.ensureError = 'Missing plugin'
  env.editor.session.pluginChain.push({ ...env.editor.session.pluginChain[0], id: 'bad' })
  await nextTick()
  const polling = env.poll()
  updateGate.resolve()
  await flush()
  assert.equal(env.playback.status, 'error')
  statusGate.resolve({ projectId: 'project', position: 2, playing: true, duration: 10 })
  await polling
  assert.equal(env.playback.status, 'error')
  assert.match(env.playback.error, /Missing plugin/)
  assert.equal(env.timers.size, 0)
})

test('a late retiring pause cannot clear a newer Web Audio failure', async t => {
  for (const pauseError of [undefined, 'Old graph is unavailable']) {
    const gate = deferred()
    const env = environment(t, { pauseGate: gate })
    await env.native.play(1)
    env.backend.pauseError = pauseError
    env.editor.session.pluginChain = []
    await flush()
    const request = env.playback.beginRequest('play', 'starting')
    env.playback.fail(request, 'New audio decoder failed')
    gate.resolve()
    await flush()
    assert.equal(env.playback.status, 'error')
    assert.equal(env.playback.error, 'New audio decoder failed')
  }
})

test('an initial load that fails after the chain was repaired retries the current chain once', async t => {
  const loadGate = deferred()
  const env = environment(t, { ensureGate: loadGate })
  env.editor.session.pluginChain.push({ ...env.editor.session.pluginChain[0], id: 'good' })
  env.backend.ensureError = 'Old bad plugin failed'
  const playing = env.native.play(1)
  env.editor.session.pluginChain.splice(0, 1)
  await flush()
  env.backend.ensureError = undefined
  loadGate.resolve()
  assert.equal(await playing, true)
  assert.equal(env.playback.error, null)
  assert.equal(env.calls.filter(call => call[0] === 'ensure').length, 2)
})

test('replacing an insert during its initial load reports the replacement failure instead of the old plugin', async t => {
  const gate = deferred()
  const env = environment(t, { ensureGate: gate })
  env.editor.session.pluginChain[0].name = 'Pro-C 3'
  env.backend.ensureError = 'Track "Drums": Pro-C 3 (old-slot): unsupported bus layout'
  const playing = env.native.play(1)
  env.editor.session.pluginChain = [{ id: 'ozone', format: 'au', pluginId: 'ozone', name: 'Ozone 12', path: 'au:ozone', bypassed: false }]
  await flush()
  env.backend.ensureError = 'Track "Vocals": Ozone 12 (ozone): initialization failed'
  gate.resolve()
  assert.equal(await playing, false)
  assert.match(env.playback.error, /Ozone 12/)
  assert.doesNotMatch(env.playback.error, /Pro-C 3/)
})

test('a plugin dialog retains playback polling and recovers without a transport error', async t => {
  const env = environment(t)
  await env.native.play(2)
  env.backend.dialogPending = true
  await env.poll()
  assert.equal(env.playback.status, 'playing')
  assert.equal(env.playback.error, null)
  assert.ok(env.timers.size)
  env.backend.dialogPending = false
  env.backend.position = 4
  await env.poll()
  assert.equal(env.playback.currentTime, 4)
  assert.equal(env.playback.error, null)
})
test('pause during a plugin dialog recovers to the actual host status', async t => {
  const env = environment(t)
  await env.native.play(2)
  env.backend.dialogPending = true
  env.native.pause()
  await flush()
  assert.equal(env.playback.error, null)
  assert.ok(env.timers.size)
  // The retained pause completes in the helper before the next status request is sent.
  env.backend.dialogPending = false; env.backend.playing = false
  await env.poll()
  assert.equal(env.playback.status, 'paused')
  assert.equal(env.playback.error, null)
})
test('pause cancels pending startup before the old request can start audio', async t => {
  const gate = deferred()
  const env = environment(t, { ensureGate: gate })
  const playing = env.native.play(3)
  env.native.pause()
  gate.resolve()
  assert.equal(await playing, false)
  assert.equal(env.playback.status, 'paused')
  assert.equal(env.calls.filter(call => call[0] === 'play').length, 0)
})
test('a status response from an old request cannot overwrite a newer seek', async t => {
  const gate = deferred()
  const env = environment(t, { statusGate: gate })
  await env.native.play(1)
  const polling = env.poll()
  env.native.seek(7)
  await flush()
  gate.resolve({ projectId: 'project', position: 2, playing: true, duration: 10 })
  await polling
  assert.equal(env.playback.currentTime, 7)
})
test('changing projects discards delayed startup and releases the previous session', async t => {
  const gate = deferred()
  const env = environment(t, { ensureGate: gate })
  const playing = env.native.play(1)
  env.editor.session = { ...env.editor.session, id: 'next' }
  await flush()
  gate.resolve()
  assert.equal(await playing, false)
  assert.ok(env.calls.some(call => call[0] === 'unload' && call[1] === 'project'))
  assert.equal(env.calls.filter(call => call[0] === 'play').length, 0)
})

test('a newer seek keeps polling after an earlier status request finishes', async t => {
  const gate = deferred()
  const env = environment(t, { statusGate: gate })
  await env.native.play(1)
  const polling = env.poll()
  env.native.seek(7)
  await flush()
  await env.poll()
  gate.resolve({ projectId: 'project', position: 2, playing: true, duration: 10 })
  await polling
  assert.equal(env.playback.currentTime, 7)
  assert.equal(env.timers.size, 1)
})

test('native playback synchronizes its transient monitor before play, seek and graph updates', async t => {
  const env = environment(t)
  env.editor.session.tracks = [{ id: 'recording', muted: false, solo: true }, { id: 'backing', muted: false, solo: false }]
  const project = JSON.stringify(env.editor.session)
  env.playback.setSuppressedTrackIds(['recording', 'recording'])
  await flush()
  assert.equal(env.calls.length, 0)
  await env.native.play(2)
  assert.deepEqual(env.calls.map(call => call[0]), ['ensure', 'monitor', 'play'])
  assert.deepEqual(Array.from(env.calls[1][2].suppressedTrackIds), ['recording'])
  env.calls.length = 0
  env.native.seek(4)
  await flush()
  assert.deepEqual(env.calls.map(call => call[0]), ['ensure', 'monitor', 'seek'])
  env.calls.length = 0
  env.playback.setSuppressedTrackIds(['backing'])
  await flush()
  assert.deepEqual(env.calls.map(call => call[0]), ['monitor'])
  assert.deepEqual(Array.from(env.calls[0][2].suppressedTrackIds), ['backing'])
  assert.equal(JSON.stringify(env.editor.session), project)
  env.calls.length = 0
  env.editor.session.tracks.push({ id: 'new-track', muted: false, solo: false })
  await flush()
  assert.deepEqual(env.calls.map(call => call[0]), ['ensure', 'monitor'])
  assert.deepEqual(Array.from(env.calls[1][2].suppressedTrackIds), ['backing'])
  assert.ok(env.calls.every(call => !call[2] || !('pcm' in call[2])))
})

test('recording owns the playhead while native status continues updating meters', async t => {
  const env = environment(t)
  env.playback.setRecordingProjectId('project')
  env.playback.setCurrentTime(12)
  assert.equal(await env.native.play(12), true)
  env.backend.position = 4
  env.playback.setCurrentTime(14)
  await env.poll()
  assert.equal(env.playback.currentTime, 14)
  assert.deepEqual(env.playback.masterLevel, [0.5, 0.25])
  assert.equal(env.playback.status, 'playing')
})

test('recording can start after accompaniment ends and continues without looping the backing', async t => {
  const env = environment(t)
  env.backend.playEnds = true
  env.playback.setRecordingProjectId('project')
  env.playback.setLoop(true)
  env.playback.setCurrentTime(12)
  assert.equal(await env.native.play(12), true)
  assert.equal(env.playback.status, 'playing')
  assert.equal(env.calls.find(call => call[0] === 'play')[2].loop, false)
  env.playback.setCurrentTime(18)
  await env.poll()
  assert.equal(env.playback.currentTime, 18)
  assert.equal(env.playback.status, 'playing')
  const plays = env.calls.filter(call => call[0] === 'play').length
  env.playback.setLoop(false)
  await flush()
  assert.equal(env.calls.filter(call => call[0] === 'play').length, plays)
})

test('a recording owner for another project does not change normal native transport behavior', async t => {
  const env = environment(t)
  env.backend.playEnds = true
  env.playback.setRecordingProjectId('another-project')
  env.playback.setCurrentTime(12)
  assert.equal(await env.native.play(12), false)
  assert.equal(env.playback.status, 'paused')
  assert.equal(env.playback.currentTime, 10)
})

test('late recording pause and old status cannot overwrite the final recording cursor or resume polling', async t => {
  const pauseGate = deferred(), statusGate = deferred()
  const env = environment(t, { pauseGate, statusGate })
  env.playback.setRecordingProjectId('project')
  await env.native.play(0)
  const polling = env.poll()
  env.playback.setCurrentTime(13)
  env.native.pause()
  env.playback.setRecordingProjectId(null)
  env.playback.setCurrentTime(14)
  pauseGate.resolve()
  await flush()
  assert.equal(env.playback.currentTime, 14)
  assert.equal(env.playback.status, 'paused')
  statusGate.resolve({ projectId: 'project', position: 5, playing: false, duration: 10 })
  await polling
  assert.equal(env.playback.currentTime, 14)
  assert.equal(env.timers.size, 0)
})

test('a recording stop with reset still leaves the final playhead to the recording owner', async t => {
  const env = environment(t)
  env.playback.setRecordingProjectId('project')
  await env.native.play(0)
  env.playback.setCurrentTime(13)
  env.native.pause(true)
  env.playback.setRecordingProjectId(null)
  await flush()
  assert.equal(env.playback.currentTime, 13)
  assert.equal(env.playback.status, 'paused')
  assert.equal(env.timers.size, 0)
})

test('recording still exposes native playback errors', async t => {
  const env = environment(t)
  env.playback.setRecordingProjectId('project')
  await env.native.play(0)
  env.backend.error = 'Audio output disconnected'
  await env.poll()
  assert.equal(env.playback.status, 'error')
  assert.equal(env.playback.error, 'Audio output disconnected')
})

test('pause cancels recording startup while its monitor request is pending', async t => {
  const monitorGate = deferred()
  const env = environment(t, { monitorGate })
  env.playback.setRecordingProjectId('project')
  const playing = env.native.play(12)
  await flush()
  env.native.pause()
  env.playback.setRecordingProjectId(null)
  monitorGate.resolve()
  assert.equal(await playing, false)
  await flush()
  assert.equal(env.calls.filter(call => call[0] === 'play').length, 0)
  assert.equal(env.playback.status, 'paused')
  assert.equal(env.timers.size, 0)
})

test('playback reset clears the recording owner and transient track suppression', () => {
  const playback = useEditorPlaybackStore(createPinia())
  playback.setRecordingProjectId('project')
  playback.setSuppressedTrackIds(['recording'])
  playback.reset()
  assert.equal(playback.recordingProjectId, null)
  assert.deepEqual(playback.suppressedTrackIds, [])
  playback.$dispose()
})

for (const hostCapabilities of [{}, { recording: false }]) {
  const capability = 'recording' in hostCapabilities ? 'disabled' : 'missing'
  test(`ordinary native playback and seek remain compatible with ${capability} recording capability`, async t => {
    const env = environment(t, { hostCapabilities })
    assert.equal(await env.native.play(2), true)
    assert.equal(env.playback.currentTime, 2)
    env.native.seek(4)
    await flush()
    assert.equal(env.playback.currentTime, 4)
    assert.equal(env.playback.status, 'playing')
    assert.ok(env.calls.some(call => call[0] === 'seek'))
    assert.equal(env.calls.filter(call => call[0] === 'monitor').length, 0)
  })

  for (const operation of ['play', 'seek']) {
    test(`native ${operation} rejects suppression with ${capability} recording capability`, async t => {
      const env = environment(t, { hostCapabilities })
      env.playback.setSuppressedTrackIds(['recording'])
      if (operation === 'play') assert.equal(await env.native.play(2), false)
      else { env.native.seek(2); await flush() }
      assert.equal(env.playback.status, 'error')
      assert.match(env.playback.error, /recordingBlocked/)
      assert.equal(env.calls.filter(call => ['monitor', 'play', 'seek'].includes(call[0])).length, 0)
    })
  }
}
