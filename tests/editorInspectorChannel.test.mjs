import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { computed, effectScope, nextTick, reactive, ref, watch } from 'vue'
import { compileScript, compileTemplate, parse } from 'vue/compiler-sfc'

const filename = 'EditorInspectorPanel.vue'
const component = readFileSync(new URL('../src/components/editor/EditorInspectorPanel.vue', import.meta.url), 'utf8')
const source = ts.createSourceFile('inspector.ts', component.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1], ts.ScriptTarget.Latest, true)
const code = ts.transpileModule(source.statements.filter(item => !ts.isImportDeclaration(item)).map(item => item.getText(source)).join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None },
}).outputText
const flush = async () => { await nextTick(); await new Promise(resolve => setImmediate(resolve)) }
function environment(t, selectedTrackId = null) {
  const props = reactive({ session: { id: 'project', name: 'Project', masterVolume: 0.8, masterPan: -0.2, sources: [], tracks: [
    { id: 'master', name: 'Voice', volume: 0.45, pan: 0.3, clips: [{ id: 'clip', start: 1, offset: 0, duration: 4, fadeIn: 0.2, fadeOut: 0.4 }] },
    { id: 'track:voice', name: 'Other', volume: 0.6, pan: -0.4, clips: [] },
  ] }, selectedTrackId, selectedClipId: 'clip', selectedSource: { id: 'source', path: '/audio.wav', duration: 5, channels: 2, sampleRate: 48000 }, duration: 5, lastExportPath: null })
  const playback = reactive({ masterLevel: [0.5, 0.25], trackLevels: { master: [0.1, 0.01], 'track:voice': [0.8, 0.4] } })
  const events = []
  const scope = effectScope()
  const context = vm.createContext({ computed, nextTick, ref, watch, defineProps: () => props,
    defineEmits: () => (...args) => events.push(args), useI18n: () => ({ t: key => key }), useEditorPlaybackStore: () => playback,
    formatTime: value => `${value}s`,
  })
  scope.run(() => vm.runInContext(code, context))
  t.after(() => scope.stop())
  return { props, playback, events,
    read: name => vm.runInContext(`${name}.value`, context),
    call(name, ...args) { context.__args = args; return vm.runInContext(`${name}(...__args)`, context) },
    set(name, value) { context.__value = value; vm.runInContext(`${name}.value = __value`, context) },
  }
}

test('master channel uses the actual session mix and cannot emit track or clip edits', t => {
  const env = environment(t)
  assert.equal(env.read('selectedTrack'), null)
  assert.equal(env.read('channelValue'), 'master')
  assert.equal(env.read('channelVolume'), 0.8)
  assert.equal(env.read('channelPan'), -0.2)
  assert.equal(env.read('selectedClip'), null)
  env.call('updateClipTiming', { start: 3 })
  env.call('updateFades', { fadeIn: 1 })
  assert.equal(env.events.length, 0)
  env.call('beginChannelVolume')
  env.call('updateChannelVolume', 1.1)
  env.call('commitChannelVolume')
  env.call('beginChannelPan')
  env.call('updateChannelPan', 0.25)
  env.call('commitChannelPan')
  assert.deepEqual(env.events, [['beginMasterVolume'], ['setMasterVolume', 1.1], ['commitMasterVolume'], ['beginMasterPan'], ['setMasterPan', 0.25], ['commitMasterPan']])
  assert.equal(env.props.session.masterVolume, 0.8, 'the inspector must use the parent/store setter')
})

test('track and master choices share one global selection and preserve real track IDs', t => {
  const env = environment(t)
  assert.deepEqual(Array.from(env.read('channelOptions'), option => option.value), ['master', 'track:master', 'track:track:voice'])
  env.call('selectChannel', 'track:master')
  env.call('selectChannel', 'master')
  env.call('selectChannel', 'track:track:voice')
  env.call('selectChannel', 'track:removed')
  assert.deepEqual(env.events, [['selectTrack', 'master'], ['selectTrack', null], ['selectTrack', 'track:voice']])
  assert.equal(env.read('channelValue'), 'master', 'a requested selection waits for the parent state update')
})

test('track controls retain their interaction boundaries and follow global selection changes', async t => {
  const env = environment(t, 'master')
  assert.equal(env.read('channelVolume'), 0.45)
  assert.equal(env.read('channelPan'), 0.3)
  assert.equal(env.read('selectedClip').id, 'clip')
  env.call('beginChannelVolume')
  env.call('updateChannelVolume', 0.7)
  env.call('commitChannelVolume')
  env.call('beginChannelPan')
  env.call('updateChannelPan', -0.1)
  env.call('commitChannelPan')
  assert.deepEqual(env.events, [['beginTrackVolume'], ['setTrackVolume', 'master', 0.7], ['commitTrackVolume'], ['beginTrackPan'], ['setTrackPan', 'master', -0.1], ['commitTrackPan']])
  env.props.selectedTrackId = 'track:voice'
  await flush()
  assert.equal(env.read('channelValue'), 'track:track:voice')
  assert.equal(env.read('channelVolume'), 0.6)
  assert.equal(env.read('selectedClip'), null)
  env.props.session.tracks.splice(1, 1)
  await flush()
  assert.equal(env.read('selectedTrack'), null)
  assert.equal(env.read('channelValue'), 'master')
  env.call('updateChannelVolume', 0.9)
  assert.deepEqual(env.events.at(-1), ['setMasterVolume', 0.9])
})

test('meters read the matching stereo levels and never invent activity or retain stale track levels', async t => {
  const env = environment(t, 'master')
  assert.deepEqual(Array.from(env.read('channelLevels')), [0.1, 0.01])
  assert.equal(env.call('formatLevel', 0.1), '-20.0')
  assert.equal(env.call('formatLevel', 0.01), '-40.0')
  assert.equal(env.call('formatLevel', 0), '−∞')
  assert.equal(env.call('meterValue', 0), -60)
  assert.equal(env.call('meterScale', 0), 0)
  assert.ok(Math.abs(env.call('meterScale', 0.1) - (0.24 + 4 / 9 * 0.16)) < 1e-12)
  env.props.selectedTrackId = null
  await flush()
  assert.deepEqual(Array.from(env.read('channelLevels')), [0.5, 0.25])
  env.playback.masterLevel = [0, 0]
  assert.deepEqual(Array.from(env.read('channelLevels')), [0, 0])
  assert.equal(env.read('channelPeak'), 0)
  env.props.selectedTrackId = 'track:voice'
  env.playback.trackLevels = {}
  assert.deepEqual(Array.from(env.read('channelLevels')), [0, 0])
  env.playback.trackLevels['track:voice'] = [NaN, Infinity]
  assert.deepEqual(Array.from(env.read('channelLevels')), [0, 0])
  env.playback.trackLevels['track:voice'] = [-0.3, 1.4]
  assert.deepEqual(Array.from(env.read('channelLevels')), [0, 1])
  assert.equal(env.events.length, 0)
})

test('meter spacing expands the upper 12 dB and keeps tick positions aligned with actual levels', t => {
  const env = environment(t)
  const ticks = [[0, 1], [-3, 0.88], [-6, 0.76], [-9, 0.64], [-12, 0.52], [-15, 0.4], [-24, 0.24], [-30, 0.12], [-60, 0]]
  for (const [db, position] of ticks) {
    assert.ok(Math.abs(env.call('meterScale', 10 ** (db / 20)) - position) < 1e-12, `${db} dB tick must match the level bar`)
  }
  assert.ok(env.call('meterDbScale', 0) - env.call('meterDbScale', -12) > 0.45)
  assert.ok(env.call('meterDbScale', -30) - env.call('meterDbScale', -60) < 0.15)
  let previous = 0
  for (let db = -72; db <= 6; db += 0.25) {
    const position = env.call('meterScale', 10 ** (db / 20))
    assert.ok(position >= previous && position >= 0 && position <= 1)
    previous = position
  }
  assert.equal(env.call('meterScale', 0), 0)
})

test('channel strip mute/solo targets only the selected track and readouts stay compact', async t => {
  const env = environment(t)
  env.call('toggleTrackFlag', 'muted')
  assert.equal(env.events.length, 0, 'the master strip has no mute/solo')
  env.props.selectedTrackId = 'track:voice'
  await flush()
  env.call('toggleTrackFlag', 'muted')
  env.call('toggleTrackFlag', 'solo')
  assert.deepEqual(env.events, [['toggleTrackFlag', 'track:voice', 'muted'], ['toggleTrackFlag', 'track:voice', 'solo']])
  assert.equal(env.call('formatVolumeDb', 1), '0.0 dB')
  assert.equal(env.call('formatVolumeDb', 2), '+6.0 dB')
  assert.equal(env.call('formatVolumeDb', 0), '−∞ dB')
  assert.equal(env.call('formatPanShort', 0.01), 'C')
  assert.equal(env.call('formatPanShort', -0.4), 'L40')
  assert.equal(env.call('formatPanShort', 1), 'R100')
})

test('global track changes cancel a pending rename without touching another channel', async t => {
  const env = environment(t, 'master')
  await env.call('beginRename')
  env.set('renameDraft', 'New name')
  env.props.selectedTrackId = null
  await flush()
  env.call('commitRename')
  assert.equal(env.events.length, 0)
  assert.equal(env.read('editingTrackId'), null)
})

test('inspector and its editor view compile with channel events and accessible controls', () => {
  for (const file of ['../src/components/editor/EditorInspectorPanel.vue', '../src/views/EditorView.vue']) {
    const filename = new URL(file, import.meta.url).pathname
    const parsed = parse(readFileSync(new URL(file, import.meta.url), 'utf8'), { filename })
    assert.deepEqual(parsed.errors, [])
    const script = compileScript(parsed.descriptor, { id: 'inspector-channel-test' })
    const result = compileTemplate({ filename, id: 'inspector-channel-test', source: parsed.descriptor.template.content,
      compilerOptions: { bindingMetadata: script.bindings, expressionPlugins: ['typescript'] } })
    assert.deepEqual(result.errors, [])
  }
})
