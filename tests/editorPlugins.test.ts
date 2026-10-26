import assert from 'node:assert/strict'
import test from 'node:test'
import type { EditorSession } from '../src/types/editor.ts'
import { applyPluginStates, clonePluginChain, hasEditorPlugins, neutralPluginRenderProject, normalizePluginChain, pluginAudioUnitVersion, pluginDescriptorCategory, pluginDescriptorFamilies, pluginDescriptorFormat, pluginDescriptorLabels, pluginDescriptorVariant, pluginProcessingProject, pluginProjectBlockReason, pluginProjectSignature, pluginProjectVersion, pluginSlotMatchesDescriptor } from '../src/utils/editorPlugins.ts'

function project(): EditorSession {
  return { id: 'project', name: 'Project', masterVolume: 0.6, masterPan: -0.4,
    tracks: [], sources: [], createdAt: 1, updatedAt: 1 }
}
const slot = { id: 'slot', format: 'vst3', pluginId: 'gain', name: 'Gain', path: 'gain.vst3', bypassed: false, state: 'sha256:old' }

test('legacy track effects do not change the audio graph or strip opaque plugin metadata', () => {
  const session = project()
  session.tracks = [{ id: 'track', sourceId: 'source', role: 'stem', name: 'Track',
    volume: 1, pan: 0, muted: false, solo: false, fadeIn: 0, fadeOut: 0,
    effects: { reverb: 0.4 }, pluginChain: [{ ...slot, effects: { vendorState: 'preserve' } }] }]
  const signature = pluginProjectSignature(session)
  const processing = pluginProcessingProject(session)
  assert.equal('effects' in processing.tracks[0], false)
  assert.deepEqual(processing.tracks[0].pluginChain![0].effects, { vendorState: 'preserve' })
  session.tracks[0].effects!.reverb = 0.8
  assert.equal(pluginProjectSignature(session), signature)
  session.tracks[0].volume = 0.5
  assert.notEqual(pluginProjectSignature(session), signature)
  assert.deepEqual(session.tracks[0].effects, { reverb: 0.8 })
})

test('library families merge one matching VST2/VST3 pair and prefer VST3 without rewriting descriptors', () => {
  const legacy = { id: 'vst2:gain', name: ' SerumFX ', vendor: ' Xfer Records ', path: '/effect.dll' }
  const modern = { id: 'vst3:gain', name: 'serumfx', vendor: 'xfer  records', path: '/effect.vst3' }
  const input = [legacy, modern]
  const families = pluginDescriptorFamilies(input)
  assert.equal(families.length, 1)
  assert.deepEqual(families[0], [modern, legacy])
  assert.equal(families[0][0], modern)
  assert.deepEqual(input, [legacy, modern])
})

test('family matching preserves vendor, channel variant and known instrument boundaries', () => {
  const legacy = { id: 'vst2:gain', name: 'Gain', vendor: 'Maker', path: '/effect.dll' }
  for (const modern of [
    { ...legacy, id: 'vst3:gain', vendor: 'Other Maker' },
    { ...legacy, id: 'vst3:gain', name: 'Gain Stereo' },
    { ...legacy, id: 'vst3:gain', isInstrument: true },
  ]) assert.equal(pluginDescriptorFamilies([legacy, modern]).length, 2)
  assert.equal(pluginDescriptorFamilies([{ ...legacy, vendor: '' }, { ...legacy, id: 'vst3:gain', vendor: '' }]).length, 2)
})

test('ambiguous same-format installations and same-name shell children remain individual choices', () => {
  const modern = { id: 'vst3:gain', name: 'Gain', vendor: 'Maker', path: '/effect.vst3' }
  const legacy = { ...modern, id: 'vst2:gain', path: '/effect.dll' }
  const duplicate = { ...modern, path: '/other/effect.vst3' }
  assert.deepEqual(pluginDescriptorFamilies([legacy, modern, duplicate]), [[legacy], [modern], [duplicate]])
  const first = { ...legacy, id: 'vst2:child-one', isShellChild: true }
  const second = { ...legacy, id: 'vst2:child-two', isShellChild: true }
  assert.deepEqual(pluginDescriptorFamilies([first, second, modern]), [[first], [second], [modern]])
})

test('Audio Units keep separate identities beside a merged VST pair', () => {
  const modern = { id: 'vst3:gain', name: 'Gain', vendor: 'Maker', path: '/effect.vst3' }
  const legacy = { ...modern, id: 'vst2:gain', path: '/effect.dll' }
  const au2 = { ...modern, id: 'au:aufx:gain:test', format: 'au' }
  const au3 = { ...au2, id: 'au3:aufx:gain:test' }
  assert.deepEqual(pluginDescriptorFamilies([legacy, au2, modern, au3]), [[modern, legacy], [au2], [au3]])
})

test('projects without plugins keep version 3 and track/master chains require version 4', () => {
  const session = project()
  assert.equal(pluginProjectVersion(session), 3)
  session.pluginChain = [slot]
  assert.equal(hasEditorPlugins(session), true)
  assert.equal(pluginProjectVersion(session), 4)
})

test('normalization and cloning preserve unknown plugin metadata and opaque states', () => {
  const unknown = { ...slot, format: 'au', state: 'opaque:unavailable', future: { value: 7 } }
  const normalized = normalizePluginChain([unknown])!
  const cloned = clonePluginChain(normalized)!
  assert.deepEqual(cloned, [unknown])
  ;(cloned[0].future as { value: number }).value = 9
  assert.equal((normalized[0].future as { value: number }).value, 7)
  assert.equal(normalizePluginChain(undefined), undefined)
})

test('unsupported active formats block processing while bypass preserves the slot', () => {
  const session = project()
  session.pluginChain = [{ ...slot, format: 'aax' }]
  assert.equal(pluginProjectBlockReason(session), 'format')
  session.pluginChain[0].bypassed = true
  assert.equal(pluginProjectBlockReason(session), null)
  assert.equal(session.pluginChain[0].format, 'aax')
})

test('snapshots preserve missing states and explicit state restoration changes the project signature', () => {
  const session = project()
  session.pluginChain = [{ ...slot }, { ...slot, id: 'missing', state: 'keep' }]
  const before = pluginProjectSignature(session)
  assert.equal(applyPluginStates(session, { slot: 'sha256:new', unrelated: 'skip' }), true)
  assert.equal(session.pluginChain[1].state, 'keep')
  assert.notEqual(pluginProjectSignature(session), before)
  assert.equal(applyPluginStates(session, { slot: 'sha256:new' }), false)
  session.pluginChain[0].bypassed = true
  assert.notEqual(pluginProjectSignature(session), before)
})

test('final encoding receives one neutral track without applying the master gain twice', () => {
  const session = project()
  session.pluginChain = [slot]
  const rendered = neutralPluginRenderProject(session, { path: 'render.wav', duration: 4.5, sampleRate: 48000, channels: 2 })
  assert.equal(rendered.masterVolume, 1)
  assert.equal(rendered.masterPan, 0)
  assert.equal(hasEditorPlugins(rendered), false)
  assert.equal(rendered.tracks[0].volume, 1)
  assert.equal(rendered.tracks[0].clips![0].duration, 4.5)
  assert.equal(rendered.sources[0].path, 'render.wav')
  assert.equal(session.masterVolume, 0.6)
  assert.equal(session.pluginChain[0], slot)
})


test('plugin relocation requires the same VST3 class and excludes instruments', () => {
  const descriptor = { id: 'gain', classId: 'gain-class', isInstrument: false }
  assert.equal(pluginSlotMatchesDescriptor(slot, descriptor), true)
  assert.equal(pluginSlotMatchesDescriptor({ ...slot, pluginId: 'gain-class' }, descriptor), true)
  assert.equal(pluginSlotMatchesDescriptor({ ...slot, pluginId: 'another-plugin' }, descriptor), false)
  assert.equal(pluginSlotMatchesDescriptor({ ...slot, classId: 'another-class' }, descriptor), false)
  assert.equal(pluginSlotMatchesDescriptor({ ...slot, format: 'au' }, descriptor), false)
  assert.equal(pluginSlotMatchesDescriptor(slot, { ...descriptor, isInstrument: true }), false)
})

test('multi-format relocation preserves format boundaries and infers legacy descriptor prefixes', () => {
  for (const format of ['vst2', 'au']) {
    const descriptor = { id: format === 'au' ? 'au:aufx:gain:test' : 'vst2:12345678', classId: 'gain-class', isInstrument: false }
    assert.equal(pluginDescriptorFormat(descriptor), format)
    assert.equal(pluginSlotMatchesDescriptor({ ...slot, format, pluginId: descriptor.id }, descriptor), true)
    assert.equal(pluginSlotMatchesDescriptor({ ...slot, pluginId: descriptor.id }, descriptor), false)
    assert.equal(pluginSlotMatchesDescriptor({ ...slot, format, pluginId: descriptor.id }, { ...descriptor, format: 'vst3' }), false)
  }
  assert.equal(pluginDescriptorFormat({ id: 'legacy-class-id' }), 'vst3')
})

test('active formats use host platform capabilities while unsupported bypassed states remain opaque', () => {
  const session = project()
  session.pluginChain = ['vst3', 'vst2', 'au'].map(format => ({ ...slot, id: format, format, state: `opaque:${format}` }))
  assert.equal(pluginProjectBlockReason(session), null)
  assert.equal(pluginProjectBlockReason(session, ['vst3', 'vst2']), 'format')
  session.pluginChain[2].bypassed = true
  assert.equal(pluginProjectBlockReason(session, ['vst3', 'vst2']), null)
  assert.equal(session.pluginChain[2].state, 'opaque:au')
})

test('project block reasons only check active plugin formats across master and track chains', () => {
  const session = project()
  session.pluginChain = [slot]
  session.tracks = [{ id: 'track', sourceId: 'source', name: 'Track', role: 'stem', volume: 1,
    pan: 0, muted: false, solo: false, fadeIn: 0, fadeOut: 0, pluginChain: [{ ...slot, id: 'track-slot' }],
    ...{ effects: { reverb: 0.2, delay: 0.3, delayTime: 0.4, clarity: 0.5, compressor: 0.6 } } }]
  assert.equal(pluginProjectBlockReason(session), null)
  assert.equal(pluginProjectBlockReason(session, ['vst3']), null)
  assert.equal(pluginProjectBlockReason(session, ['vst2']), 'format')
  session.pluginChain[0].bypassed = true
  assert.equal(pluginProjectBlockReason(session, ['vst2']), 'format')
  session.tracks[0].pluginChain![0].bypassed = true
  assert.equal(pluginProjectBlockReason(session, ['vst2']), null)
})

test('AUv3 and AUv2 identities parse their complete fourcc fields and infer one AU format', () => {
  for (const [prefix, version] of [['au', 2], ['au3', 3]] as const) {
    for (const suffix of ['aufx:gain:test', 'aumf:A:B::Test', '#61756D75:#00000001:test']) {
      const id = `${prefix}:${suffix}`
      assert.equal(pluginAudioUnitVersion(id), version)
      assert.equal(pluginDescriptorFormat({ id }), 'au')
      assert.equal(pluginDescriptorVariant({ id }), `AUv${version}`)
    }
    for (const suffix of ['system', 'aufx:x:test', 'aufx:gain:test:extra', 'xxxx:gain:test', 'aufx:éabc:test', 'aufx:#0000000Z:test']) {
      const id = `${prefix}:${suffix}`
      assert.equal(pluginAudioUnitVersion(id), null)
      assert.equal(pluginDescriptorFormat({ id }), 'vst3')
    }
  }
})

test('AUv2 state cannot relocate into AUv3 with the same component codes or shared class alias', () => {
  const descriptor = { id: 'au3:aufx:gain:test', classId: 'au:aufx:gain:test', isInstrument: false, format: 'au' }
  const saved = { ...slot, format: 'au', pluginId: 'au:aufx:gain:test', path: 'au:aufx:gain:test' }
  assert.equal(pluginSlotMatchesDescriptor(saved, descriptor), false)
  assert.equal(pluginSlotMatchesDescriptor({ ...saved, pluginId: descriptor.id }, descriptor), true)
})

test('shell children with unknown classification remain selectable while known instruments are excluded', () => {
  const descriptor = { id: 'vst2:12345678', classId: 'vst2:12345678', isInstrument: null, format: 'vst2' }
  const saved = { ...slot, format: 'vst2', pluginId: descriptor.id }
  assert.equal(pluginSlotMatchesDescriptor(saved, descriptor), true)
  assert.equal(pluginSlotMatchesDescriptor(saved, { ...descriptor, isInstrument: false }), true)
  assert.equal(pluginSlotMatchesDescriptor(saved, { ...descriptor, isInstrument: true }), false)
})

test('library labels distinguish plugin variants, same-name shell children and duplicate installation paths', () => {
  const common = { name: 'Gain', vendor: 'Vendor', path: 'effect.plugin' }
  const descriptors = [
    { ...common, id: 'vst3-class', format: 'vst3' },
    { ...common, id: 'vst2:12345678', format: 'vst2' },
    { ...common, id: 'au:aufx:gain:test' },
    { ...common, id: 'au3:aufx:gain:test' },
    { ...common, id: 'vst2:00000001', isShellChild: true },
    { ...common, id: 'vst2:00000002', isShellChild: true },
    { ...common, id: 'vst3-class', format: 'vst3', path: 'other/effect.plugin' },
  ]
  const labels = pluginDescriptorLabels(descriptors)
  assert.equal(new Set(labels).size, descriptors.length)
  assert.match(labels[2], /AUv2/)
  assert.match(labels[3], /AUv3/)
  assert.match(labels[4], /VST2 Shell.*vst2:00000001/)
  assert.match(labels[5], /VST2 Shell.*vst2:00000002/)
  assert.ok(labels[0].endsWith('effect.plugin'))
  assert.ok(labels[6].endsWith('other/effect.plugin'))
})

test('normalization preserves AU variant, shell classification and future plugin metadata', () => {
  const saved = [
    { ...slot, format: 'au', pluginId: 'au3:aufx:gain:test', auVersion: 3, state: 'opaque:au3' },
    { ...slot, format: 'vst2', pluginId: 'vst2:12345678', isShellChild: true, isInstrument: null, future: { revision: 4 } },
  ]
  assert.deepEqual(clonePluginChain(normalizePluginChain(saved)), saved)
})

test('effect categories use declared VST3 types and coarse VST2 values', () => {
  assert.equal(pluginDescriptorCategory({ id: 'gain', category: 'Eq' }), 'eq')
  assert.equal(pluginDescriptorCategory({ id: 'meter', category: 'Other', subCategories: ['Fx', 'Analyzer'] }), 'analyzer')
  assert.equal(pluginDescriptorCategory({ id: 'delay', subCategories: ['Fx|Delay'] }), 'delay')
  assert.equal(pluginDescriptorCategory({ id: 'vst2:room', rawCategory: 6 }), 'ambience')
  assert.equal(pluginDescriptorCategory({ id: 'vst2:master', rawCategory: 4 }), 'mastering')
  assert.equal(pluginDescriptorCategory({ id: 'vst2:unknown', rawCategory: -1 }), 'other')
  assert.equal(pluginDescriptorCategory({ id: 'vst2:shell-child', isShellChild: true, rawCategory: null }), 'other')
  assert.equal(pluginDescriptorCategory({ id: 'vst2:shell-child', isShellChild: true, rawCategory: 6 }), 'other')
})

test('names, unknown tokens and inferred AU categories do not invent effect classifications', () => {
  const descriptor = { id: 'gain', name: 'Delay Reverb EQ', path: '/Equalizers/Reverb.vst3' }
  assert.equal(pluginDescriptorCategory(descriptor), 'other')
  assert.equal(pluginDescriptorCategory({ ...descriptor, category: 'A custom category', subCategories: ['Fx', 'CustomVendorTag'] }), 'other')
  for (const token of ['constructor', '__proto__', 'toString']) {
    assert.equal(pluginDescriptorCategory({ ...descriptor, category: token, subCategories: ['Fx', token] }), 'other')
  }
  assert.equal(pluginDescriptorCategory({ id: 'au:aufx:test:Make', category: 'Reverb', componentType: 0x61756678 }), 'other')
  assert.equal(pluginDescriptorCategory({ id: 'au3:aumu:test:Make', componentType: 0x61756d75 }), 'instrument')
  assert.equal(pluginDescriptorCategory({ id: 'synth', isInstrument: true }), 'instrument')
  assert.equal(pluginDescriptorCategory({ id: 'synth', subCategories: ['Instrument', 'Synth'] }), 'instrument')
})
