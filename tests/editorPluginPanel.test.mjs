import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { compileTemplate, parse } from 'vue/compiler-sfc'
import { computed, effectScope, nextTick, proxyRefs, reactive, ref, shallowRef, watch } from 'vue'
import { clonePluginChain, pluginDescriptorCategory, pluginDescriptorFamilies, pluginDescriptorFormat, pluginFavoriteKey, pluginDescriptorLabels, pluginDescriptorVariant, pluginSlotMatchesDescriptor } from '../src/utils/editorPlugins.ts'

const component = readFileSync(new URL('../src/components/editor/EditorPluginPanel.vue', import.meta.url), 'utf8')
const script = component.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
const source = ts.createSourceFile('panel.ts', script, ts.ScriptTarget.Latest, true)
const code = ts.transpileModule(source.statements.filter(item => !ts.isImportDeclaration(item)).map(item => item.getText(source)).join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None },
}).outputText
const flush = async () => { await nextTick(); await new Promise(resolve => setImmediate(resolve)) }
function deferred() { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }
const plugin = id => ({ id, format: 'vst3', pluginId: id, name: id, path: `/${id}.vst3`, bypassed: false })
function environment(t, { scanControl = false } = {}) {
  const capture = deferred()
  const props = reactive({ session: { id: 'project', tracks: [], pluginChain: ['a', 'b', 'c'].map(plugin) }, selectedTrackId: null })
  const writes = [], calls = [], emitted = [], mounts = [], disposals = [], timers = new Map(), windowListeners = new Map()
  let timerSequence = 0, stopped = false
  const preferences = new Map()
  const controls = { scanControl, hostCapabilities: {}, scanStatus: null, statusGate: null, skipGate: null, skipResult: true, cancelResult: true, blacklist: [], blacklistGate: null, directory: null, cacheGate: null, cacheReport: { plugins: [], failures: [], scanned: 0, cached: 0, hasCache: false }, scanGate: null, editorGate: null, captureGate: capture, report: {
    plugins: [{ ...plugin('library'), id: 'vst3:library', classId: 'vst3:library', vendor: 'Vendor', version: '1', isInstrument: false }],
    failures: [], scanned: 1, cached: 0,
  } }
  const context = vm.createContext({
    PluginHostDialogPendingError: class PluginHostDialogPendingError extends Error {},
    usePluginManagerLayout: () => ({}),
    usePluginManagerWindow: () => ({}),
    defineEmits: () => (event, trackId) => emitted.push({ event, trackId }),
    computed, ref, shallowRef, watch, nextTick, defineProps: () => props, useI18n: () => ({ t: key => key, locale: ref('en') }),
    useEditorStore: () => reactive({
      async capturePluginState(project, force, recovery) { calls.push({ kind: 'capture', project, force, recovery }); await controls.captureGate.promise; if (controls.captureError) throw controls.captureError },
      async recoverPluginChain(options) {
        calls.push({ kind: 'recover', options })
        const target = options.trackId === null ? props.session : props.session.tracks.find(track => track.id === options.trackId)
        writes.push({ trackId: options.trackId, chain: options.chain })
        target.pluginChain = options.chain
      },
      setPluginChain(trackId, chain) {
        writes.push({ trackId, chain })
        const target = trackId ? props.session.tracks.find(track => track.id === trackId) : props.session
        if (target) target.pluginChain = chain
      },
      scheduleSave() { calls.push({ kind: 'save', project: props.session }) },
    }),
    useEditorPlaybackStore: () => reactive({ error: null }), observePluginSession: () => () => {}, onScopeDispose: callback => disposals.push(callback), onMounted: callback => mounts.push(callback),
    currentPluginSession: () => controls.host || null,
    pluginHostRequest: async () => { if (controls.probeUnavailable && controls.host) { controls.host.unavailable = true; throw new Error('Host is closed') } },
    async invoke(command, args) {
      calls.push({ kind: 'invoke', command, args })
      if (command === 'pick_input_folder') return controls.directory
      if (command === 'plugin_host_cached_scan_report') {
        const report = controls.cacheReport
        if (controls.cacheGate) await controls.cacheGate.promise
        return report
      }
      if (command === 'plugin_host_scan_status') {
        const result = typeof controls.scanStatus === 'function' ? controls.scanStatus(args) : controls.scanStatus
        if (controls.statusGate) await controls.statusGate.promise
        return result
      }
      if (command === 'plugin_host_skip_scan') {
        const skipped = controls.skipResult
        if (controls.skipGate) await controls.skipGate.promise
        return { skipped }
      }
      if (command === 'plugin_host_cancel_scan') return { cancelled: controls.cancelResult }
      if (command === 'plugin_host_scan_blacklist') {
        const entries = [...controls.blacklist]
        if (controls.blacklistGate) await controls.blacklistGate.promise
        return entries
      }
      if (command === 'plugin_host_remove_scan_blacklist') { controls.blacklist = controls.blacklist.filter(entry => entry.path !== args.path); return }
      assert.equal(command, 'plugin_host_scan_report')
      const report = controls.report
      if (controls.scanGate) await controls.scanGate.promise
      return report
    },
    async openPluginEditor(project, slotId, open) {
      calls.push({ kind: 'editor', project, slotId, open })
      if (controls.editorGate) await controls.editorGate.promise
      return { open }
    },
    pluginHostCapabilities: async () => ({ enabled: true, platform: 'windows', formats: ['vst3'], nativeEditor: true, scanControl: controls.scanControl, catalogCache: true, ...controls.hostCapabilities }),
    crypto: { randomUUID: () => 'added-slot' },
    localStorage: { getItem: key => preferences.get(key) ?? null, setItem: (key, value) => preferences.set(key, value) },
    window: { addEventListener: (name, callback) => windowListeners.set(name, callback), removeEventListener: name => windowListeners.delete(name) },
    setTimeout: callback => { timers.set(++timerSequence, callback); return timerSequence },
    clearTimeout: id => timers.delete(id),
    clonePluginChain, pluginDescriptorCategory, pluginDescriptorFamilies, pluginDescriptorFormat, pluginFavoriteKey, pluginDescriptorLabels, pluginDescriptorVariant, pluginSlotMatchesDescriptor,
  })
  const scope = effectScope()
  scope.run(() => vm.runInContext(code, context))
  const dispose = () => { if (!stopped) { stopped = true; disposals.forEach(callback => callback()); scope.stop() } }
  t.after(dispose)
  const read = name => vm.runInContext(`${name}.value`, context)
  const set = (name, value) => { context.__value = value; vm.runInContext(`${name}.value = __value`, context) }
  const call = (name, ...args) => { context.__args = args; return vm.runInContext(`${name}(...__args)`, context) }
  set('capabilities', { enabled: true, formats: ['vst3'], nativeEditor: true, scanControl })
  function trigger(action) {
    context.slot = props.session.pluginChain[1]
    context.index = 1
    const control = [...component.matchAll(/<n-(button|checkbox)\b[\s\S]*?<\/n-\1>/g)]
      .map(match => match[0]).find(markup => markup.includes(`t('editor.plugins.${action}')`))
    assert.ok(control, `Missing ${action} action`)
    const expression = control.match(action === 'bypass' ? /@update:checked="([^"]+)"/ : /@click="([^"]+)"/)[1]
    const eventCode = ts.transpileModule(`const handler = ${action === 'bypass' ? expression : `() => ${expression}`}; handler`, {
      compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None },
    }).outputText
    return vm.runInContext(eventCode, context)(true)
  }
  return { props, writes, calls, emitted, controls, preferences, mounts, timers, windowListeners, dispose, read, set, call, trigger, releaseCapture: capture.resolve,
    async tick() { const pending = [...timers.values()]; timers.clear(); pending.forEach(callback => callback()); await flush() },
    evaluate: expression => vm.runInContext(expression, context),
    buttonDisabled(key) {
      const button = [...component.matchAll(/<n-button\b[\s\S]*?<\/n-button>/g)].map(match => match[0]).find(markup => markup.includes(`t('editor.plugins.${key}')`))
      assert.ok(button, `Missing ${key} button`)
      const expression = button.match(/:disabled="([^"]+)"/)[1]
      context.__render = proxyRefs(vm.runInContext('({busy,scanning,canSkipScan,canStopScan,scanActionPending,scanStopRequested})', context))
      return vm.runInContext(`with (__render) { (${expression}) }`, context)
    },
  }
}

test('dead hosts require confirmation before removing or quarantining an insert', async t => {
  for (const action of ['remove', 'bypass']) {
    const env = environment(t)
    env.controls.captureError = new Error('Host is closed')
    const host = env.controls.host = { project: env.props.session, sessionId: 'dead', unavailable: true }
    env.releaseCapture()
    if (action === 'remove') await env.call('removePlugin', 'b')
    else await env.call('setPluginBypassed', 'b', true)
    assert.equal(env.writes.length, 0)
    assert.equal(env.read('recoveryEdit').host, host, 'confirmation must retain the actual session identity')
    await env.call('confirmRecoveryEdit')
    assert.equal(env.calls.filter(call => call.kind === 'recover').length, 1)
    const chain = env.props.session.pluginChain
    if (action === 'remove') assert.deepEqual(chain.map(slot => slot.id), ['a', 'c'])
    else assert.equal(chain.find(slot => slot.id === 'b').skipLoad, true)
    assert.equal(env.read('recoveryEdit'), null)
  }
})

test('cancelled or obsolete recovery confirmation cannot modify the plugin chain', async t => {
  const env = environment(t)
  env.controls.captureError = new Error('Host is closed')
  env.controls.host = { project: env.props.session, sessionId: 'dead', unavailable: true }
  env.releaseCapture()
  await env.call('removePlugin', 'b')
  env.set('recoveryEdit', null)
  assert.deepEqual(env.props.session.pluginChain.map(slot => slot.id), ['a', 'b', 'c'])
  await env.call('removePlugin', 'b')
  env.props.session.pluginChain.push(plugin('new'))
  await env.call('confirmRecoveryEdit')
  assert.equal(env.calls.some(call => call.kind === 'recover'), false)
  assert.deepEqual(env.props.session.pluginChain.map(slot => slot.id), ['a', 'b', 'c', 'new'])
})

test('a responsive host capture failure does not offer destructive recovery', async t => {
  const env = environment(t)
  env.controls.captureError = new Error('Incomplete snapshot')
  env.controls.host = { project: env.props.session, sessionId: 'alive', unavailable: false }
  env.releaseCapture()
  await env.call('removePlugin', 'b')
  assert.equal(env.read('recoveryEdit'), null)
  assert.equal(env.writes.length, 0)
})

test('switching projects cancels a pending unavailable-host confirmation', async t => {
  const env = environment(t)
  env.controls.captureError = new Error('Host is closed')
  env.controls.host = { project: env.props.session, sessionId: 'dead', unavailable: true }
  env.releaseCapture()
  await env.call('removePlugin', 'b')
  assert.ok(env.read('recoveryEdit'))
  env.props.session = { ...env.props.session, id: 'replacement' }
  await flush()
  assert.equal(env.read('recoveryEdit'), null)
  assert.equal(env.writes.length, 0)
})

test('enabling an explicitly quarantined plugin clears its load suppression', async t => {
  const env = environment(t)
  env.props.session.pluginChain[1].bypassed = true
  env.props.session.pluginChain[1].skipLoad = true
  env.releaseCapture()
  await env.call('setPluginBypassed', 'b', false)
  assert.equal(env.props.session.pluginChain[1].bypassed, false)
  assert.equal(env.props.session.pluginChain[1].skipLoad, undefined)
})

test('dismissing an error keeps the underlying persistence failure and new failures become visible', async t => {
  const env = environment(t)
  env.evaluate("editor.pluginStateError = 'Unable to save plugin state'")
  await flush()
  assert.equal(env.read('showOperationError'), true)
  env.call('dismissOperationError')
  assert.equal(env.read('showOperationError'), false)
  assert.equal(env.evaluate('editor.pluginStateError'), 'Unable to save plugin state')
  env.evaluate("editor.pluginStateError = 'New plugin state failure'")
  await flush()
  assert.equal(env.read('showOperationError'), true)
  env.call('dismissOperationError')
  await env.call('run', () => {})
  assert.equal(env.read('showOperationError'), true, 'a new action must expose an unresolved failure again')
  env.evaluate('editor.pluginStateError = null')
  await flush()
  assert.equal(env.read('showOperationError'), false)
})

test('catalog searches names, vendors and formats and clears a hidden or unsupported selection', async t => {
  const env = environment(t)
  const plugins = [
    { id: 'gain', name: 'Precision Gain', vendor: 'Audio Tools', path: '/gain.vst3', format: 'vst3' },
    { id: 'echo', name: 'Stereo Echo', vendor: 'Room Audio', path: '/echo.dll', format: 'vst2' },
    { id: 'synth', name: 'Keyboard', vendor: 'Audio Tools', path: '/synth.vst3', format: 'vst3', isInstrument: true },
  ]
  env.set('descriptors', plugins)
  env.set('selectedPluginId', JSON.stringify(['gain', '/gain.vst3']))
  await flush()
  env.set('librarySearch', '  PRECISION ')
  assert.equal(env.read('pluginOptions').length, 1)
  env.set('librarySearch', 'room audio')
  await flush()
  assert.equal(env.read('selectedPluginId'), null)
  assert.equal(env.read('pluginOptions')[0].plugin.id, 'echo')
  assert.equal(env.read('pluginOptions')[0].disabled, true)
  env.set('librarySearch', 'VST3')
  assert.equal(env.read('pluginOptions').length, 2)
  assert.equal(env.read('pluginOptions').find(option => option.plugin.id === 'synth').disabled, true)
  env.set('selectedPluginId', JSON.stringify(['gain', '/gain.vst3']))
  env.set('capabilities', { enabled: true, formats: ['vst2'] })
  await flush()
  assert.equal(env.read('selectedPluginId'), null)
  assert.equal(env.writes.length, 0)
})

test('one VST2/VST3 library row defaults to the supported VST3 descriptor when adding an effect', async t => {
  const env = environment(t)
  env.set('capabilities', { enabled: true, formats: ['vst2', 'vst3'] })
  env.set('descriptors', [
    { id: 'vst2:serum', name: 'SerumFX', vendor: 'Xfer', format: 'vst2', path: '/SerumFX.dll' },
    { id: 'vst3:serum', name: 'SerumFX', vendor: 'Xfer', format: 'vst3', path: '/SerumFX.vst3' },
  ])
  const options = env.read('pluginOptions')
  assert.equal(options.length, 1)
  assert.equal(options[0].formatLabel, 'VST3 / VST2')
  assert.equal(env.read('catalogGroups')[0].plugins.length, 1)
  env.call('selectCatalogPlugin', options[0])
  assert.equal(env.read('selectedDescriptor').format, 'vst3')
  assert.equal(env.read('selectedFormatOptions').length, 2)
  env.releaseCapture()
  await env.call('addPlugin')
  const added = env.writes[0].chain.at(-1)
  assert.equal(added.format, 'vst3')
  assert.equal(added.pluginId, 'vst3:serum')
  assert.equal(added.path, '/SerumFX.vst3')
})

test('a merged row permits explicit VST2 selection and searching either format retains one result', async t => {
  const env = environment(t)
  env.set('capabilities', { enabled: true, formats: ['vst2', 'vst3'] })
  env.set('descriptors', [
    { id: 'vst2:gain', name: 'Gain', vendor: 'Maker', format: 'vst2', path: '/Gain.dll' },
    { id: 'vst3:gain', name: 'Gain', vendor: 'Maker', format: 'vst3', path: '/Gain.vst3' },
  ])
  env.call('selectCatalogPlugin', env.read('pluginOptions')[0])
  const legacyKey = JSON.stringify(['vst2:gain', '/Gain.dll'])
  env.call('selectPluginFormat', legacyKey)
  await flush()
  env.call('selectCatalogPlugin', env.read('pluginOptions')[0])
  assert.equal(env.read('selectedPluginId'), legacyKey)
  assert.equal(env.call('isCatalogPluginSelected', env.read('pluginOptions')[0]), true)
  for (const query of ['VST2', 'VST3']) {
    env.set('librarySearch', query)
    await flush()
    assert.equal(env.read('pluginOptions').length, 1)
    assert.equal(env.read('selectedPluginId'), legacyKey)
  }
  env.releaseCapture()
  await env.call('addPlugin')
  const added = env.writes[0].chain.at(-1)
  assert.equal(added.format, 'vst2')
  assert.equal(added.pluginId, 'vst2:gain')
  assert.equal(added.path, '/Gain.dll')
})

test('merged rows fall back to a supported format and relocation never changes a saved format', async t => {
  const env = environment(t)
  env.set('capabilities', { enabled: true, formats: ['vst2'] })
  const legacy = { id: 'vst2:gain', classId: 'legacy', name: 'Gain', vendor: 'Maker', format: 'vst2', path: '/Gain.dll', isInstrument: false }
  env.set('descriptors', [legacy, { ...legacy, id: 'vst3:gain', classId: 'modern', format: 'vst3', path: '/Gain.vst3' }])
  env.call('selectCatalogPlugin', env.read('pluginOptions')[0])
  assert.equal(env.read('selectedDescriptor').format, 'vst2')
  env.call('selectPluginFormat', JSON.stringify(['vst3:gain', '/Gain.vst3']))
  assert.equal(env.read('selectedDescriptor').format, 'vst2')
  env.props.session.pluginChain = [{ ...legacy, pluginId: legacy.id, id: 'saved', state: 'sha256:legacy', bypassed: true }]
  env.call('beginRelocation', 'saved')
  const options = env.read('pluginOptions')
  assert.equal(options.length, 1)
  assert.equal(options[0].variants.length, 1)
  assert.equal(options[0].plugin.format, 'vst2')
  assert.equal(env.props.session.pluginChain[0].state, 'sha256:legacy')
  assert.equal(env.writes.length, 0)
})

test('double-click activation adds once while capture is pending and respects the selected format', async t => {
  const env = environment(t)
  env.set('capabilities', { enabled: true, formats: ['vst2', 'vst3'] })
  env.set('descriptors', [
    { id: 'vst2:gain', name: 'Gain', vendor: 'Maker', format: 'vst2', path: '/Gain.dll' },
    { id: 'vst3:gain', name: 'Gain', vendor: 'Maker', format: 'vst3', path: '/Gain.vst3' },
  ])
  const option = env.read('pluginOptions')[0]
  env.call('selectCatalogPlugin', option)
  assert.equal(env.writes.length, 0, 'a single click only selects the effect')
  env.call('selectPluginFormat', JSON.stringify(['vst2:gain', '/Gain.dll']))
  const adding = env.call('activateCatalogPlugin', option)
  await env.call('activateCatalogPlugin', option)
  await flush()
  assert.equal(env.calls.filter(call => call.kind === 'capture').length, 1)
  env.releaseCapture()
  await adding
  assert.equal(env.writes.length, 1)
  assert.equal(env.writes[0].chain.at(-1).format, 'vst2')
  assert.equal(env.writes[0].chain.at(-1).path, '/Gain.dll')
})

test('double-clicking an unsupported library effect cannot start a capture or edit the chain', async t => {
  const env = environment(t)
  env.set('descriptors', [{ id: 'instrument', name: 'Synth', vendor: 'Maker', path: '/Synth.vst3', isInstrument: true }])
  await env.call('activateCatalogPlugin', env.read('pluginOptions')[0])
  assert.equal(env.calls.length, 0)
  assert.equal(env.writes.length, 0)
  assert.equal(env.read('selectedPluginId'), null)
})

test('double-click during relocation updates the saved instance without adding another plugin', async t => {
  const env = environment(t)
  env.props.session.pluginChain = [{ id: 'saved', pluginId: 'effect', format: 'vst3', name: 'Effect',
    path: '/old.vst3', bypassed: true, state: 'sha256:preserved', vendorMetadata: { keep: true } }]
  env.set('descriptors', [{ id: 'effect', classId: 'effect', format: 'vst3', name: 'Effect',
    vendor: 'Maker', path: '/new.vst3', isInstrument: false }])
  env.call('beginRelocation', 'saved')
  env.releaseCapture()
  await env.call('activateCatalogPlugin', env.read('pluginOptions')[0])
  assert.equal(env.writes.length, 1)
  assert.equal(env.writes[0].chain.length, 1)
  assert.equal(env.writes[0].chain[0].id, 'saved')
  assert.equal(env.writes[0].chain[0].path, '/new.vst3')
  assert.equal(env.writes[0].chain[0].state, 'sha256:preserved')
  assert.equal(env.writes[0].chain[0].vendorMetadata.keep, true)
})

test('vendor grouping merges casing and whitespace, sorts names and keeps unknown vendors last', async t => {
  const env = environment(t)
  const plugins = [
    { id: 'late', name: 'Zeta', vendor: ' Vendor ', path: '/zeta.vst3' },
    { id: 'first', name: 'Alpha', vendor: 'vendor', path: '/alpha.vst3' },
    { id: 'unknown', name: 'Unlabelled', vendor: '', path: '/unknown.vst3' },
    { id: 'second', name: 'Other', vendor: 'Another', path: '/other.vst3' },
  ]
  env.set('descriptors', plugins)
  const groups = env.read('catalogGroups')
  assert.deepEqual(Array.from(groups, group => group.key), ['vendor:another', 'vendor:vendor', 'vendor:'])
  assert.deepEqual(Array.from(groups[1].plugins, option => option.plugin.name), ['Alpha', 'Zeta'])
  env.set('selectedPluginId', JSON.stringify(['late', '/zeta.vst3']))
  env.set('catalogGroupBy', 'format')
  await flush()
  assert.equal(env.read('selectedPluginId'), JSON.stringify(['late', '/zeta.vst3']))
  assert.equal(env.read('catalogGroups').length, 1)
  assert.equal(env.read('catalogGroups')[0].label, 'VST3')
  assert.equal(env.writes.length, 0)
})

test('catalog groups start collapsed and toggling never changes the selected effect or project', t => {
  const env = environment(t)
  env.set('descriptors', [
    { id: 'gain', name: 'Gain', vendor: 'Audio Tools', path: '/gain.vst3' },
    { id: 'eq', name: 'EQ', vendor: 'Audio Tools', path: '/eq.vst3' },
    { id: 'echo', name: 'Echo', vendor: 'Room Audio', path: '/echo.vst3' },
  ])
  const keys = Array.from(env.read('catalogGroups'), group => group.key)
  assert.ok(keys.every(key => env.call('isCatalogGroupExpanded', key) === false))
  env.set('selectedPluginId', JSON.stringify(['gain', '/gain.vst3']))
  env.call('toggleCatalogGroup', keys[0])
  assert.equal(env.call('isCatalogGroupExpanded', keys[0]), true)
  assert.equal(env.call('isCatalogGroupExpanded', keys[1]), false)
  env.call('toggleCatalogGroup', keys[0])
  assert.equal(env.call('isCatalogGroupExpanded', keys[0]), false)
  assert.equal(env.read('selectedPluginId'), JSON.stringify(['gain', '/gain.vst3']))
  assert.equal(env.calls.length, 0)
  assert.equal(env.writes.length, 0)
})

test('search expands matches, permits temporary collapsing and restores browsing state when cleared', async t => {
  const env = environment(t)
  env.set('descriptors', [
    { id: 'gain', name: 'Gain', vendor: 'Audio Tools', path: '/gain.vst3' },
    { id: 'echo', name: 'Echo', vendor: 'Room Audio', path: '/echo.vst3' },
  ])
  const [first, second] = Array.from(env.read('catalogGroups'), group => group.key)
  env.call('toggleCatalogGroup', first)
  env.set('librarySearch', 'VST3')
  await flush()
  assert.equal(env.call('isCatalogGroupExpanded', first), true)
  assert.equal(env.call('isCatalogGroupExpanded', second), true)
  env.call('toggleCatalogGroup', second)
  assert.equal(env.call('isCatalogGroupExpanded', second), false)
  env.set('librarySearch', 'Echo')
  await flush()
  assert.equal(env.read('catalogGroups').length, 1)
  assert.equal(env.call('isCatalogGroupExpanded', second), true)
  env.set('librarySearch', '  ')
  await flush()
  assert.equal(env.call('isCatalogGroupExpanded', first), true)
  assert.equal(env.call('isCatalogGroupExpanded', second), false)
  assert.equal(env.calls.length, 0)
})

test('vendor, category and format grouping retain independent expansion choices', t => {
  const env = environment(t)
  env.set('descriptors', [{ id: 'gain', name: 'Gain', vendor: 'Maker', path: '/gain.vst3', category: 'Eq' }])
  const vendorKey = env.read('catalogGroups')[0].key
  env.call('toggleCatalogGroup', vendorKey)
  env.set('catalogGroupBy', 'category')
  const categoryKey = env.read('catalogGroups')[0].key
  assert.equal(env.call('isCatalogGroupExpanded', categoryKey), false)
  env.call('toggleCatalogGroup', categoryKey)
  env.set('catalogGroupBy', 'format')
  const formatKey = env.read('catalogGroups')[0].key
  assert.equal(env.call('isCatalogGroupExpanded', formatKey), false)
  env.set('catalogGroupBy', 'vendor')
  assert.equal(env.call('isCatalogGroupExpanded', vendorKey), true)
  env.set('catalogGroupBy', 'category')
  assert.equal(env.call('isCatalogGroupExpanded', categoryKey), true)
  assert.equal(env.calls.length, 0)
})

test('effect groups and category search use reported metadata and never infer types from names', async t => {
  const env = environment(t)
  env.set('descriptors', [
    { id: 'eq', name: 'Channel', vendor: 'Maker', path: '/eq.vst3', category: 'Eq', subCategories: ['Fx', 'EQ'] },
    { id: 'room', name: 'Room', vendor: 'Maker', path: '/room.dll', format: 'vst2', rawCategory: 6 },
    { id: 'unknown', name: 'Magic EQ Reverb Delay', vendor: '', path: '/unknown.vst3' },
  ])
  env.set('catalogGroupBy', 'category')
  assert.deepEqual(Array.from(env.read('catalogGroups'), group => group.key).sort(), ['category:ambience', 'category:eq', 'category:other'])
  assert.equal(env.read('catalogGroups').at(-1).key, 'category:other')
  env.set('selectedPluginId', JSON.stringify(['room', '/room.dll']))
  env.set('librarySearch', 'EQ')
  await flush()
  assert.equal(env.read('selectedPluginId'), null)
  assert.equal(env.read('pluginOptions').length, 2)
  assert.ok(env.read('catalogGroups').every(group => group.plugins.every(option => option.plugin.id !== 'room')))
  assert.equal(env.writes.length, 0)
})

test('relocation resets the search and only offers the same plugin identity and format', async t => {
  const env = environment(t)
  env.set('descriptors', [
    { id: 'a', name: 'Gain', vendor: 'Vendor', path: '/new/a.vst3', format: 'vst3' },
    { id: 'a', name: 'Gain', vendor: 'Vendor', path: '/a.dll', format: 'vst2' },
    { id: 'another', name: 'Gain', vendor: 'Vendor', path: '/another.vst3', format: 'vst3' },
  ])
  env.set('librarySearch', 'previous search')
  env.call('beginRelocation', 'a')
  assert.equal(env.read('librarySearch'), '')
  assert.equal(env.read('pluginOptions').length, 1)
  assert.equal(env.read('pluginOptions')[0].plugin.path, '/new/a.vst3')
  assert.equal(env.writes.length, 0)
})

test('catalog exposes AU versions, shell identities and duplicate locations as visible metadata', t => {
  const env = environment(t)
  env.set('capabilities', { enabled: true, formats: ['vst3', 'vst2', 'au'] })
  env.set('descriptors', [
    { id: 'au:aufx:gain:Test', name: 'Gain', vendor: 'Vendor', path: '/au', format: 'au', auVersion: 2 },
    { id: 'au3:aufx:gain:Test', name: 'Gain', vendor: 'Vendor', path: '/au3', format: 'au', auVersion: 3 },
    { id: 'vst2:one', name: 'Shell Gain', vendor: 'Vendor', path: '/shell.dll', format: 'vst2', isShellChild: true },
    { id: 'vst2:two', name: 'Shell Gain', vendor: 'Vendor', path: '/shell.dll', format: 'vst2', isShellChild: true },
    { id: 'gain', name: 'Gain', vendor: 'Vendor', path: '/first/gain.vst3', format: 'vst3' },
    { id: 'gain', name: 'Gain', vendor: 'Vendor', path: '/second/gain.vst3', format: 'vst3' },
  ])
  const options = env.read('pluginOptions')
  assert.equal(options[0].variant, 'AUv2')
  assert.equal(options[1].variant, 'AUv3')
  assert.equal(options[2].variant, 'VST2 Shell')
  assert.equal(options[2].detail, 'vst2:one')
  assert.equal(options[3].detail, 'vst2:two')
  assert.equal(options[4].detail, 'gain · /first/gain.vst3')
  assert.equal(options[5].detail, 'gain · /second/gain.vst3')
})

test('browsing folders preserves existing paths, ignores cancellation and avoids duplicates', async t => {
  const env = environment(t)
  env.set('directories', ['/existing'])
  env.controls.directory = '/additional'
  await env.call('browseDirectory')
  await env.call('browseDirectory')
  env.controls.directory = null
  await env.call('browseDirectory')
  assert.deepEqual(JSON.parse(JSON.stringify(env.read('directories'))), ['/existing', '/additional'])
  assert.deepEqual(JSON.parse(env.preferences.get('pymss-plugin-library-directories-v1')), ['/existing', '/additional'])
  assert.equal(env.read('directoryDraft'), '')
  assert.equal(env.read('busy'), false)
  assert.equal(env.calls.filter(call => call.command === 'pick_input_folder').length, 3)
  assert.equal(env.calls.filter(call => call.command === 'plugin_host_scan_report').length, 2)
  assert.deepEqual(JSON.parse(JSON.stringify(env.calls.filter(call => call.command === 'plugin_host_scan_report')[0].args.paths)), ['/existing', '/additional'])
  assert.equal(env.writes.length, 0)
})

test('empty sources cannot invoke discovery, including a forced retry', async t => {
  const env = environment(t)
  await env.call('scan')
  await env.call('scan', true)
  assert.equal(env.calls.length, 0)
  assert.equal(env.read('scanning'), false)
  assert.equal(env.read('scanReport'), null)
})

test('first launch ignores legacy fixture paths and only restores explicitly selected sources', async t => {
  const env = environment(t)
  env.preferences.set('pymss-vst-lab-search-paths', JSON.stringify(['/old-development-fixtures']))
  await env.mounts[0]()
  assert.equal(env.read('directories').length, 0)
  assert.equal(env.calls.filter(call => call.command !== 'plugin_host_cached_scan_report').length, 0)
  env.preferences.set('pymss-plugin-library-directories-v1', JSON.stringify(['/chosen', '/chosen', '', 'au:system']))
  await env.mounts[0]()
  assert.deepEqual(JSON.parse(JSON.stringify(env.read('directories'))), ['/chosen'])
  assert.equal(env.calls.filter(call => call.command !== 'plugin_host_cached_scan_report').length, 0)
  assert.ok(env.preferences.has('pymss-vst-lab-search-paths'))
})

for (const [platform, defaults] of [
  ['windows', ['C:\\Program Files\\Common Files\\VST3', 'C:\\Program Files\\VstPlugins']],
  ['macos', ['/Users/musician/Library/Audio/Plug-Ins/VST3', '/Users/musician/Library/Audio/Plug-Ins/VST', '/Library/Audio/Plug-Ins/VST3', '/Library/Audio/Plug-Ins/VST']],
]) {
  test(`${platform} seeds host-provided standard directories without starting a scan`, async t => {
    const env = environment(t)
    env.controls.hostCapabilities = { platform, defaultDirectories: defaults }
    await env.mounts[0]()
    assert.deepEqual(JSON.parse(JSON.stringify(env.read('directories'))), defaults)
    assert.equal(env.calls.filter(call => call.command !== 'plugin_host_cached_scan_report').length, 0)
    assert.equal(env.read('scanning'), false)
    assert.equal(env.writes.length, 0)
  })
}

test('saved custom or explicitly empty sources override defaults and survive remounting', async t => {
  const env = environment(t)
  env.controls.hostCapabilities = { defaultDirectories: ['C:\\Program Files\\Common Files\\VST3', 'C:\\Program Files\\VstPlugins'] }
  const key = 'pymss-plugin-library-directories-v1'
  for (const selected of [['E:\\Effects'], []]) {
    env.preferences.set(key, JSON.stringify(selected))
    await env.mounts[0]()
    assert.deepEqual(JSON.parse(JSON.stringify(env.read('directories'))), selected)
    assert.equal(env.preferences.get(key), JSON.stringify(selected))
  }
  assert.equal(env.calls.filter(call => call.command !== 'plugin_host_cached_scan_report').length, 0)
})

test('adding defaults preserves custom sources, avoids duplicates and persists without probing', async t => {
  const env = environment(t)
  env.controls.hostCapabilities = { defaultDirectories: ['/standard/vst3', '/standard/vst2'] }
  const key = 'pymss-plugin-library-directories-v1'
  env.preferences.set(key, JSON.stringify(['/custom', '/standard/vst3']))
  await env.mounts[0]()
  assert.equal(env.read('hasMissingDefaultDirectories'), true)
  env.call('addDefaultDirectories')
  env.call('addDefaultDirectories')
  const expected = ['/custom', '/standard/vst3', '/standard/vst2']
  assert.deepEqual(JSON.parse(JSON.stringify(env.read('directories'))), expected)
  assert.deepEqual(JSON.parse(env.preferences.get(key)), expected)
  assert.equal(env.read('hasMissingDefaultDirectories'), false)
  await env.mounts[0]()
  assert.deepEqual(JSON.parse(JSON.stringify(env.read('directories'))), expected)
  assert.equal(env.calls.filter(call => call.command !== 'plugin_host_cached_scan_report').length, 0)
  assert.equal(env.writes.length, 0)
})

test('invalid preferences fall back to defaults while disabled hosts never seed directories', async t => {
  const env = environment(t)
  const key = 'pymss-plugin-library-directories-v1'
  env.controls.hostCapabilities = { defaultDirectories: ['/standard'] }
  for (const invalid of ['{broken', 'null', '{}']) {
    env.preferences.set(key, invalid)
    await env.mounts[0]()
    assert.deepEqual(JSON.parse(JSON.stringify(env.read('directories'))), ['/standard'])
  }
  env.preferences.delete(key)
  env.controls.hostCapabilities.enabled = false
  await env.mounts[0]()
  assert.equal(env.read('directories').length, 0)
  assert.equal(env.calls.filter(call => call.command !== 'plugin_host_cached_scan_report').length, 0)
})

test('refresh replaces cached library entries, drops removed selections and leaves project inserts intact', async t => {
  const env = environment(t)
  const removed = { ...plugin('removed'), vendor: 'Removed Vendor', state: 'sha256:retained-project-state' }
  const retained = { ...plugin('retained'), vendor: 'Existing Vendor' }
  const added = { ...plugin('added'), vendor: 'New Vendor' }
  env.props.session.pluginChain = [removed]
  const projectBefore = JSON.stringify(env.props.session)
  env.set('directories', ['/plugins'])
  env.controls.report = { plugins: [removed, retained], failures: [], scanned: 2, cached: 0 }
  await env.trigger('scan')
  env.set('selectedPluginId', JSON.stringify([removed.id, removed.path]))
  env.controls.report = { plugins: [retained, added], failures: [], scanned: 1, cached: 1 }
  await env.call('scan')
  await flush()
  assert.deepEqual(Array.from(env.read('descriptors'), plugin => plugin.id), ['retained', 'added'])
  assert.deepEqual(Array.from(env.read('catalogGroups'), group => group.label), ['Existing Vendor', 'New Vendor'])
  assert.equal(env.read('selectedPluginId'), null)
  assert.equal(env.read('scanReport').cached, 1)
  assert.equal(JSON.stringify(env.props.session), projectBefore)
  assert.equal(env.writes.length, 0)
  const scans = env.calls.filter(call => call.command === 'plugin_host_scan_report')
  assert.equal(scans.length, 2)
  assert.ok(scans.every(call => call.args.force === false))
})

test('source changes discard an in-flight catalog and removing the last source keeps existing effects', async t => {
  const env = environment(t)
  env.set('directories', ['/first'])
  env.controls.scanGate = deferred()
  const scanning = env.call('scan')
  await flush()
  env.set('directories', ['/second'])
  env.controls.scanGate.resolve()
  await scanning
  assert.equal(env.read('scanReport'), null)
  assert.equal(env.read('descriptors').length, 0)
  env.set('descriptors', env.controls.report.plugins)
  env.set('selectedPluginId', JSON.stringify(['vst3:library', '/library.vst3']))
  const before = env.calls.length
  env.call('removeDirectory', 0)
  await flush()
  assert.equal(env.read('directories').length, 0)
  assert.equal(env.read('descriptors').length, 0)
  assert.equal(env.read('selectedPluginId'), null)
  assert.equal(env.calls.length, before)
  assert.deepEqual(env.props.session.pluginChain.map(slot => slot.id), ['a', 'b', 'c'])
  assert.equal(env.writes.length, 0)
})

test('Audio Unit discovery requires an explicit supported source choice', async t => {
  const env = environment(t)
  env.set('capabilities', { enabled: true, platform: 'windows', formats: ['vst3'] })
  await env.call('chooseAudioUnits')
  assert.equal(env.calls.length, 0)
  env.set('capabilities', { enabled: true, platform: 'macos', formats: ['vst3', 'au'] })
  await env.call('chooseAudioUnits')
  await env.call('chooseAudioUnits')
  assert.deepEqual(JSON.parse(JSON.stringify(env.read('directories'))), ['au:system'])
  assert.deepEqual(JSON.parse(JSON.stringify(env.calls[0].args.paths)), ['au:system'])
  assert.equal(env.calls.filter(call => call.command === 'plugin_host_scan_report').length, 2)
})

for (const action of ['bypass', 'remove', 'moveUp']) {
  test(`${action} addresses the selected slot after the chain changes during capture`, async t => {
    const env = environment(t)
    env.trigger(action)
    await flush()
    const [a, b, c] = env.props.session.pluginChain
    env.props.session.pluginChain = [c, a, b]
    env.releaseCapture()
    await flush()
    if (action === 'remove') assert.deepEqual(env.props.session.pluginChain.map(slot => slot.id), ['c', 'a'])
    else if (action === 'moveUp') assert.deepEqual(env.props.session.pluginChain.map(slot => slot.id), ['c', 'b', 'a'])
    else {
      assert.equal(env.props.session.pluginChain.find(slot => slot.id === 'b').bypassed, true)
      assert.ok(env.props.session.pluginChain.filter(slot => slot.id !== 'b').every(slot => !slot.bypassed))
    }
  })

  test(`${action} does not edit a different slot if its target disappears during capture`, async t => {
    const env = environment(t)
    env.trigger(action)
    await flush()
    env.props.session.pluginChain = env.props.session.pluginChain.filter(slot => slot.id !== 'b')
    env.releaseCapture()
    await flush()
    assert.deepEqual(env.props.session.pluginChain.map(slot => slot.id), ['a', 'c'])
    assert.ok(env.props.session.pluginChain.every(slot => !slot.bypassed))
    assert.equal(env.writes.length, 0)
  })
}

test('a chain action cannot cross a project switch during capture', async t => {
  const env = environment(t)
  env.trigger('remove')
  await flush()
  env.props.session = { id: 'next', tracks: [], pluginChain: ['a', 'b', 'c'].map(plugin) }
  env.releaseCapture()
  await flush()
  assert.deepEqual(env.props.session.pluginChain.map(slot => slot.id), ['a', 'b', 'c'])
  assert.equal(env.writes.length, 0)
})

test('library target selection emits the channel choice and follows external track selection without an echo', async t => {
  const env = environment(t)
  env.props.session.tracks.push({ id: 'voice', name: 'Voice', pluginChain: [plugin('voice-effect')] })
  env.set('target', 'track:voice')
  await flush()
  assert.deepEqual(env.emitted, [{ event: 'selectTarget', trackId: 'voice' }])
  env.props.selectedTrackId = 'voice'
  await flush()
  assert.equal(env.emitted.length, 1)
  env.props.selectedTrackId = null
  await flush()
  assert.equal(env.read('target'), 'master')
  assert.equal(env.emitted.length, 1)
  assert.equal(env.writes.length, 0)
})

test('a track named master by identifier retains its own inserts instead of selecting the output bus', async t => {
  const env = environment(t)
  env.props.session.tracks.push({ id: 'master', name: 'Audio', pluginChain: [plugin('track-effect')] })
  env.props.selectedTrackId = 'master'
  await flush()
  assert.equal(env.read('target'), 'track:master')
  assert.equal(env.read('chain')[0].id, 'track-effect')
  env.set('target', 'master')
  await flush()
  assert.deepEqual(env.emitted, [{ event: 'selectTarget', trackId: null }])
  assert.equal(env.read('chain')[0].id, 'a')
})

test('closing and reopening the manager preserves its library, paths and target without touching native state', async t => {
  const env = environment(t)
  env.props.session.tracks.push({ id: 'voice', name: 'Voice', pluginChain: [plugin('voice-effect')] })
  env.set('target', 'track:voice')
  env.set('directories', ['/Library/Audio/Plug-Ins/VST3'])
  env.set('descriptors', env.controls.report.plugins)
  env.set('scanReport', env.controls.report)
  await flush()
  env.call('openManager', 'voice-effect')
  assert.equal(env.read('managerOpen'), true)
  assert.equal(env.read('parameterSlotId'), 'voice-effect')
  env.call('closeManager')
  assert.equal(env.read('managerOpen'), false)
  assert.equal(env.read('parameterSlotId'), 'voice-effect')
  env.call('openManager')
  assert.equal(env.read('managerOpen'), true)
  assert.equal(env.read('target'), 'track:voice')
  assert.deepEqual(JSON.parse(JSON.stringify(env.read('directories'))), ['/Library/Audio/Plug-Ins/VST3'])
  assert.deepEqual(JSON.parse(JSON.stringify(env.read('descriptors'))), env.controls.report.plugins)
  assert.deepEqual(JSON.parse(JSON.stringify(env.read('scanReport'))), env.controls.report)
  assert.equal(env.calls.length, 0)
  assert.equal(env.writes.length, 0)
})

test('a scan finishing after the manager closes keeps its result for reopening', async t => {
  const env = environment(t)
  env.controls.scanGate = deferred()
  env.set('directories', ['/plugins'])
  env.call('openManager')
  const scanning = env.call('scan', true)
  await flush()
  env.call('closeManager')
  env.controls.scanGate.resolve()
  await scanning
  assert.equal(env.read('managerOpen'), false)
  assert.deepEqual(JSON.parse(JSON.stringify(env.read('descriptors'))), env.controls.report.plugins)
  assert.deepEqual(JSON.parse(JSON.stringify(env.read('scanReport'))), env.controls.report)
  env.call('openManager')
  assert.equal(env.read('descriptors').length, 1)
  assert.equal(env.calls.filter(call => call.command === 'plugin_host_scan_report').length, 1)
  assert.equal(env.calls.some(call => call.kind === 'editor' || call.kind === 'capture'), false)
})

test('a project replacement preserves the library while discarding a late scan for the old project', async t => {
  const env = environment(t)
  env.controls.scanGate = deferred()
  env.call('openManager', 'b')
  env.set('selectedPluginId', 'selected')
  env.set('relocatingSlotId', 'b')
  env.set('directories', ['/plugins'])
  env.set('descriptors', env.controls.report.plugins)
  env.set('scanReport', env.controls.report)
  const scanning = env.call('scan')
  await flush()
  env.props.session = { ...env.props.session, pluginChain: [plugin('next')] }
  await flush()
  assert.equal(env.read('managerOpen'), false)
  assert.equal(env.read('parameterSlotId'), null)
  assert.equal(env.read('selectedPluginId'), null)
  assert.equal(env.read('relocatingSlotId'), null)
  env.controls.scanGate.resolve()
  await scanning
  assert.equal(env.read('descriptors').length, 1)
  assert.equal(env.read('scanReport').plugins.length, 1)
  assert.deepEqual(JSON.parse(JSON.stringify(env.read('directories'))), ['/plugins'])
})

test('changing the target clears generic parameters and deleting the active track falls back to master', async t => {
  const env = environment(t)
  env.props.session.tracks.push({ id: 'voice', name: 'Voice', pluginChain: [plugin('voice-effect')] })
  env.set('target', 'track:voice')
  await flush()
  env.call('openManager', 'voice-effect')
  env.set('target', 'master')
  await flush()
  assert.equal(env.read('parameterSlotId'), null)
  env.set('target', 'track:voice')
  await flush()
  env.call('openManager', 'voice-effect')
  env.props.session.tracks = []
  await flush()
  assert.equal(env.read('target'), 'master')
  assert.equal(env.read('parameterSlotId'), null)
  assert.equal(env.writes.length, 0)
})

test('adding a plugin cannot write a removed track after asynchronous state capture', async t => {
  const env = environment(t)
  env.props.session.tracks.push({ id: 'voice', name: 'Voice', pluginChain: [] })
  env.set('target', 'track:voice')
  await flush()
  env.set('descriptors', env.controls.report.plugins)
  env.set('selectedPluginId', JSON.stringify(['vst3:library', '/library.vst3']))
  const adding = env.call('addPlugin')
  await flush()
  assert.equal(env.calls.filter(call => call.kind === 'capture').length, 1)
  env.props.session.tracks = []
  await flush()
  env.releaseCapture()
  await adding
  assert.equal(env.writes.length, 0)
  assert.deepEqual(env.props.session.pluginChain.map(slot => slot.id), ['a', 'b', 'c'])
})

for (const open of [true, false]) {
  test(`late native editor ${open ? 'open' : 'close'} cannot mutate or save a replacement project`, async t => {
    const env = environment(t)
    env.controls.editorGate = deferred()
    env.releaseCapture()
    env.set('opened', open ? [] : ['b'])
    const previous = env.props.session
    const toggling = env.call('toggleEditor', 'b')
    await flush()
    assert.equal(env.calls.filter(call => call.kind === 'editor').length, 1)
    assert.equal(env.calls[0].project, previous)
    env.props.session = { ...previous, pluginChain: [plugin('next')] }
    await flush()
    env.controls.editorGate.resolve()
    await toggling
    assert.equal(env.read('opened').length, 0)
    assert.equal(env.calls.some(call => call.kind === 'capture' || call.kind === 'save'), false)
    assert.equal(env.writes.length, 0)
  })
}

test('closing a native editor cannot save a replacement project after its state capture completes', async t => {
  const env = environment(t)
  env.set('opened', ['b'])
  const previous = env.props.session
  const closing = env.call('toggleEditor', 'b')
  await flush()
  const captures = env.calls.filter(call => call.kind === 'capture')
  assert.equal(captures.length, 1)
  assert.equal(captures[0].project, previous)
  env.props.session = { ...previous, pluginChain: [plugin('next')] }
  await flush()
  env.releaseCapture()
  await closing
  assert.equal(env.calls.some(call => call.kind === 'save'), false)
  assert.equal(env.read('opened').length, 0)
})

const activeScanStatus = (scanId, path = '/slow.vst3') => ({ scanId, phase: 'scanning', currentPath: path,
  currentName: path.split('/').at(-1), elapsedMs: 3400, completed: 2, total: 7, canSkip: true })

test('controlled scanning starts progress observation before discovery and busy controls remain operable', async t => {
  const env = environment(t, { scanControl: true })
  env.set('directories', ['/plugins'])
  env.controls.scanGate = deferred()
  env.controls.scanStatus = ({ scanId }) => activeScanStatus(scanId)
  const scanning = env.call('scan', true)
  await flush()
  const commands = env.calls.filter(call => call.kind === 'invoke')
  assert.equal(commands[0].command, 'plugin_host_scan_status')
  assert.equal(commands[1].command, 'plugin_host_scan_report')
  assert.equal(commands[0].args.scanId, commands[1].args.scanId)
  assert.equal(commands[1].args.force, true)
  assert.equal(env.read('busy'), false)
  assert.equal(env.read('scanStatus').elapsedMs, 3400)
  assert.equal(env.read('scanStatus').completed, 2)
  assert.equal(env.buttonDisabled('skipScanPlugin'), false)
  assert.equal(env.buttonDisabled('stopScan'), false)
  await env.call('skipCurrentScanPlugin')
  const skipped = env.calls.find(call => call.command === 'plugin_host_skip_scan')
  assert.deepEqual(JSON.parse(JSON.stringify(skipped.args)), { scanId: commands[1].args.scanId, path: '/slow.vst3' })
  await env.call('stopScan')
  assert.equal(env.read('scanStopRequested'), true)
  assert.equal(env.buttonDisabled('skipScanPlugin'), true)
  assert.equal(env.buttonDisabled('stopScan'), true)
  env.controls.report.cancelled = true
  env.controls.scanGate.resolve()
  await scanning
  assert.equal(env.read('busy'), false)
  assert.equal(env.read('scanning'), false)
  assert.equal(env.read('scanReport').cancelled, true)
  assert.equal(env.timers.size, 0)
  assert.equal(env.writes.length, 0)
  assert.deepEqual(env.props.session.pluginChain.map(slot => slot.id), ['a', 'b', 'c'])
})

test('an older backend receives only the original scan call and never the optional controls', async t => {
  const env = environment(t)
  await env.mounts[0]()
  env.set('capabilities', { enabled: true, formats: ['vst3'], nativeEditor: true })
  env.set('directories', ['/plugins'])
  await env.call('scan')
  assert.deepEqual(env.calls.map(call => call.command), ['plugin_host_scan_report'])
  assert.equal('scanId' in env.calls[0].args, false)
  assert.equal(env.timers.size, 0)
})

test('disposing a controlled scan cancels its own identifier and discards late status and results', async t => {
  const env = environment(t, { scanControl: true })
  env.set('directories', ['/plugins'])
  env.controls.scanGate = deferred()
  env.controls.statusGate = deferred()
  env.controls.scanStatus = ({ scanId }) => activeScanStatus(scanId)
  const scanning = env.call('scan')
  await flush()
  const scanId = env.calls.find(call => call.command === 'plugin_host_scan_report').args.scanId
  env.dispose()
  assert.equal(env.read('busy'), false)
  assert.equal(env.timers.size, 0)
  assert.equal(env.calls.find(call => call.command === 'plugin_host_cancel_scan').args.scanId, scanId)
  env.controls.statusGate.resolve()
  env.controls.scanGate.resolve()
  await scanning
  await flush()
  assert.equal(env.read('scanStatus'), null)
  assert.equal(env.read('scanReport'), null)
  assert.equal(env.read('descriptors').length, 0)
  assert.equal(env.timers.size, 0)
  assert.equal(env.calls.filter(call => call.command === 'plugin_host_cancel_scan').length, 1)
  assert.equal(env.writes.length, 0)
})

test('a stale skip targets the displayed plugin path and cannot silently skip the next plugin', async t => {
  const env = environment(t, { scanControl: true })
  env.set('directories', ['/plugins'])
  env.controls.scanGate = deferred()
  env.controls.scanStatus = ({ scanId }) => activeScanStatus(scanId, '/first.vst3')
  const scanning = env.call('scan')
  await flush()
  env.controls.scanStatus = ({ scanId }) => activeScanStatus(scanId, '/next.vst3')
  env.controls.skipResult = false
  await env.call('skipCurrentScanPlugin')
  assert.equal(env.calls.find(call => call.command === 'plugin_host_skip_scan').args.path, '/first.vst3')
  assert.equal(env.read('scanFeedback'), 'editor.plugins.scanPluginAlreadyFinished')
  assert.equal(env.read('scanBlacklist').length, 0)
  await env.tick()
  assert.equal(env.read('scanStatus').currentPath, '/next.vst3')
  assert.equal(env.calls.filter(call => call.command === 'plugin_host_skip_scan').length, 1)
  env.controls.scanGate.resolve()
  await scanning
  assert.equal(env.writes.length, 0)
})

test('a closed manager keeps its controlled scan and library selection state until the report completes', async t => {
  const env = environment(t, { scanControl: true })
  env.props.session.tracks.push({ id: 'voice', name: 'Voice', pluginChain: [plugin('voice-effect')] })
  env.set('target', 'track:voice')
  env.set('directories', ['/plugins'])
  env.set('catalogGroupBy', 'category')
  env.set('librarySearch', 'Vendor')
  env.controls.scanStatus = ({ scanId }) => activeScanStatus(scanId)
  env.controls.scanGate = deferred()
  env.call('openManager')
  const scanning = env.call('scan')
  await flush()
  env.call('closeManager')
  await env.tick()
  assert.equal(env.read('managerOpen'), false)
  assert.equal(env.read('busy'), false)
  assert.equal(env.calls.filter(call => call.command === 'plugin_host_cancel_scan').length, 0)
  assert.equal(env.calls.filter(call => call.command === 'plugin_host_scan_status').length, 2)
  env.controls.scanGate.resolve()
  await scanning
  assert.equal(env.timers.size, 0)
  env.call('openManager')
  assert.equal(env.read('target'), 'track:voice')
  assert.equal(env.read('catalogGroupBy'), 'category')
  assert.equal(env.read('librarySearch'), 'Vendor')
  assert.equal(env.read('descriptors').length, 1)
  assert.equal(env.writes.length, 0)
})

test('late status, skip and scan replies cannot clear or overwrite a newer project scan', async t => {
  const env = environment(t, { scanControl: true })
  env.set('directories', ['/plugins'])
  const oldScan = deferred(), oldStatus = deferred(), oldSkip = deferred()
  env.controls.scanGate = oldScan
  env.controls.scanStatus = ({ scanId }) => activeScanStatus(scanId, '/old.vst3')
  const oldRequest = env.call('scan')
  await flush()
  const oldId = env.read('scanStatus').scanId
  env.controls.statusGate = oldStatus
  await env.tick()
  env.controls.skipGate = oldSkip
  const skipping = env.call('skipCurrentScanPlugin')
  await flush()
  env.props.session = { ...env.props.session, id: 'next', pluginChain: [plugin('untouched')] }
  await flush()
  assert.equal(env.calls.find(call => call.command === 'plugin_host_cancel_scan').args.scanId, oldId)
  env.controls.statusGate = null
  env.controls.skipGate = null
  env.controls.scanGate = deferred()
  env.controls.report = { ...env.controls.report, plugins: [{ ...env.controls.report.plugins[0], name: 'New catalog' }] }
  env.controls.scanStatus = ({ scanId }) => activeScanStatus(scanId, '/new.vst3')
  const newRequest = env.call('scan')
  await flush()
  const newId = env.read('scanStatus').scanId
  assert.notEqual(newId, oldId)
  oldStatus.resolve()
  oldSkip.resolve()
  oldScan.resolve()
  await oldRequest
  await skipping
  await flush()
  assert.equal(env.read('busy'), false)
  assert.equal(env.read('scanning'), true)
  assert.equal(env.read('scanStatus').scanId, newId)
  assert.equal(env.read('scanStatus').currentPath, '/new.vst3')
  assert.equal(env.read('scanFeedback'), '')
  assert.equal(env.timers.size, 1)
  env.controls.scanGate.resolve()
  await newRequest
  assert.equal(env.read('descriptors')[0].name, 'New catalog')
  assert.equal(env.timers.size, 0)
  assert.deepEqual(env.props.session.pluginChain.map(slot => slot.id), ['untouched'])
  assert.equal(env.writes.length, 0)
})

test('blacklisted entries survive forced scans and removing one requires an explicit later rescan', async t => {
  const env = environment(t, { scanControl: true })
  const entry = { path: '/blocked.vst3', name: 'Blocked', reason: 'manual', addedAt: 123 }
  env.controls.blacklist = [entry]
  await env.call('refreshBlacklist')
  assert.equal(env.read('scanBlacklist')[0].path, entry.path)
  env.set('directories', ['/plugins'])
  env.controls.report = { ...env.controls.report, blacklisted: [entry], cancelled: false }
  await env.call('scan', true)
  await flush()
  assert.equal(env.read('scanBlacklist').length, 1)
  const report = env.calls.find(call => call.command === 'plugin_host_scan_report')
  assert.equal(report.args.force, true)
  assert.deepEqual(Object.keys(report.args).sort(), ['force', 'paths', 'scanId'])
  await env.call('removeBlacklistedPlugin', entry.path)
  assert.equal(env.read('scanBlacklist').length, 0)
  assert.equal(env.calls.filter(call => call.command === 'plugin_host_scan_report').length, 1)
  assert.equal(env.calls.find(call => call.command === 'plugin_host_remove_scan_blacklist').args.path, entry.path)
  env.controls.report = { ...env.controls.report, blacklisted: [] }
  await env.call('scan')
  assert.equal(env.calls.filter(call => call.command === 'plugin_host_scan_report').length, 2)
  assert.deepEqual(env.props.session.pluginChain.map(slot => slot.id), ['a', 'b', 'c'])
  assert.equal(env.writes.length, 0)
})


test('a delayed blacklist refresh cannot resurrect an entry after the user removes it', async t => {
  const env = environment(t, { scanControl: true })
  const entry = { path: '/blocked.vst3', name: 'Blocked', reason: 'timeout', addedAt: 123 }
  env.controls.blacklist = [entry]
  await env.call('refreshBlacklist')
  env.controls.blacklistGate = deferred()
  const staleRefresh = env.call('refreshBlacklist')
  await flush()
  await env.call('removeBlacklistedPlugin', entry.path)
  env.controls.blacklistGate.resolve()
  await staleRefresh
  assert.equal(env.read('scanBlacklist').length, 0)
  assert.equal(env.calls.filter(call => call.command === 'plugin_host_scan_report').length, 0)
  assert.equal(env.writes.length, 0)
})

test('system Audio Units skips the explicit metadata source rather than claiming a particular component', async t => {
  const env = environment(t, { scanControl: true })
  env.set('capabilities', { enabled: true, platform: 'macos', formats: ['au'], nativeEditor: true, scanControl: true })
  env.set('directories', ['au:system'])
  env.controls.scanGate = deferred()
  env.controls.scanStatus = ({ scanId }) => ({ ...activeScanStatus(scanId, 'au:system'), currentName: 'Audio Units' })
  const scanning = env.call('scan')
  await flush()
  assert.equal(env.call('scanItemName', 'au:system', 'Audio Units'), 'editor.plugins.systemAudioUnits')
  assert.equal(env.call('scanItemName', '/shell.dll', 'Shell Bundle'), 'Shell Bundle')
  await env.call('skipCurrentScanPlugin')
  assert.equal(env.calls.find(call => call.command === 'plugin_host_skip_scan').args.path, 'au:system')
  assert.equal(env.writes.length, 0)
  env.controls.scanGate.resolve()
  await scanning
})

test('scan controls and blacklist preserve a valid accessible component template', () => {
  const parsed = parse(component, { filename: 'EditorPluginPanel.vue' })
  assert.deepEqual(parsed.errors, [])
  const result = compileTemplate({ filename: 'EditorPluginPanel.vue', id: 'scan-controls-test',
    source: parsed.descriptor.template.content, compilerOptions: { expressionPlugins: ['typescript'] } })
  assert.deepEqual(result.errors, [])
})

test('null and terminal scan status cleanly gate controls without completing the pending report early', async t => {
  const env = environment(t, { scanControl: true })
  env.set('directories', ['/plugins'])
  env.controls.scanGate = deferred()
  const scanning = env.call('scan')
  await flush()
  assert.equal(env.buttonDisabled('skipScanPlugin'), true)
  assert.equal(env.buttonDisabled('stopScan'), false)
  await env.call('skipCurrentScanPlugin')
  assert.equal(env.calls.filter(call => call.command === 'plugin_host_skip_scan').length, 0)
  env.controls.scanStatus = ({ scanId }) => ({ ...activeScanStatus(scanId), phase: 'finished', currentPath: null, canSkip: false })
  await env.tick()
  assert.equal(env.read('busy'), false)
  assert.equal(env.timers.size, 0)
  assert.equal(env.buttonDisabled('stopScan'), true)
  env.controls.scanGate.resolve()
  await scanning
  assert.equal(env.read('busy'), false)
  assert.equal(env.timers.size, 0)
})

test('generic parameters stay collapsed until requested and follow slot IDs across reordering', async t => {
  const env = environment(t)
  assert.equal(env.read('parameterSlotId'), null)
  assert.equal(env.read('parameterSlot'), undefined)
  env.trigger('parameters')
  assert.equal(env.read('parameterSlotId'), 'b')
  assert.equal(env.read('parameterSlot').id, 'b')
  env.props.session.pluginChain = [env.props.session.pluginChain[1], env.props.session.pluginChain[0]]
  await flush()
  assert.equal(env.read('parameterSlot').id, 'b')
  env.props.session.pluginChain.splice(0, 1)
  await flush()
  assert.equal(env.read('parameterSlotId'), null)
  assert.equal(env.read('parameterSlot'), undefined)
  env.call('toggleParameters', 'no-longer-present')
  assert.equal(env.read('parameterSlotId'), null)
  assert.equal(env.calls.length, 0)
  assert.equal(env.writes.length, 0)
})

test('source management consumes Escape before the modeless workbench and never cancels background scanning', async t => {
  const env = environment(t, { scanControl: true })
  env.set('directories', ['/plugins'])
  env.controls.scanGate = deferred()
  const scanning = env.call('scan')
  await flush()
  env.call('openManager')
  env.set('sourcesOpen', true)
  let prevented = false, stopped = false
  env.call('onManagerKeyCapture', { key: 'Escape', defaultPrevented: false, preventDefault() { prevented = true }, stopPropagation() { stopped = true } })
  assert.equal(prevented, true)
  assert.equal(stopped, true)
  assert.equal(env.read('sourcesOpen'), false)
  assert.equal(env.read('managerOpen'), true)
  assert.equal(env.read('scanning'), true)
  assert.equal(env.calls.filter(call => call.command === 'plugin_host_cancel_scan').length, 0)
  env.call('closeManager')
  assert.equal(env.read('sourcesOpen'), false)
  assert.equal(env.read('scanning'), true)
  env.controls.scanGate.resolve()
  await scanning
})

test('primary pointer resizing dismisses transient menus without changing the project', t => {
  const env = environment(t)
  env.call('openManager')
  const menus = ['sourcesOpen', 'targetMenuOpen', 'reloadConfirmOpen']
  for (const control of ['window', 'library', 'parameters']) {
    menus.forEach(name => env.set(name, true))
    env.call('onManagerPointerCapture', { button: 0, target: { closest: () => ({ dataset: { pluginResize: control } }) } })
    menus.forEach(name => assert.equal(env.read(name), false))
    assert.equal(env.read('managerOpen'), true)
  }
  menus.forEach(name => env.set(name, true))
  env.call('onManagerPointerCapture', { button: 2, target: { closest: () => ({ dataset: { pluginResize: 'window' } }) } })
  env.call('onManagerPointerCapture', { button: 0, target: { closest: () => null } })
  menus.forEach(name => assert.equal(env.read(name), true))
  assert.equal(env.calls.length, 0)
  assert.equal(env.writes.length, 0)
})

test('only supported resize keys dismiss menus before the child geometry handler', t => {
  const env = environment(t)
  const menus = ['sourcesOpen', 'targetMenuOpen', 'reloadConfirmOpen']
  for (const [control, key, prevented, dismiss] of [
    ['window', 'ArrowUp', false, true], ['library', 'Home', false, true], ['parameters', 'End', false, true],
    ['window', 'Home', false, false], ['library', 'ArrowUp', false, false], ['parameters', 'ArrowRight', false, false],
    ['window', 'Tab', false, false], ['window', 'ArrowUp', true, false],
  ]) {
    menus.forEach(name => env.set(name, true))
    env.call('onManagerKeyCapture', { key, defaultPrevented: prevented, target: { closest: () => ({ dataset: { pluginResize: control } }) } })
    menus.forEach(name => assert.equal(env.read(name), !dismiss))
  }
  assert.equal(env.calls.length, 0)
  assert.equal(env.writes.length, 0)
})

test('viewport resize dismisses menus and disposal releases the resize listener', async t => {
  const env = environment(t)
  for (const mount of env.mounts) await mount()
  env.call('openManager')
  const menus = ['sourcesOpen', 'targetMenuOpen', 'reloadConfirmOpen']
  menus.forEach(name => env.set(name, true))
  env.windowListeners.get('resize')()
  menus.forEach(name => assert.equal(env.read(name), false))
  assert.equal(env.read('managerOpen'), true)
  env.dispose()
  assert.equal(env.windowListeners.size, 0)
  assert.equal(env.writes.length, 0)
})

test('remount restores a large disk catalog without starting a plugin scan or touching the project', async t => {
  const plugins = Array.from({ length: 400 }, (_, i) => ({ ...plugin(`cached-${i}`), name: `Effect ${i}`, vendor: `Vendor ${i % 20}` }))
  const report = { plugins, failures: [], scanned: 0, cached: 400, hasCache: true }
  const first = environment(t)
  first.preferences.set('pymss-plugin-library-directories-v1', JSON.stringify(['/plugins']))
  first.controls.cacheReport = report
  await first.mounts[0]()
  assert.equal(first.read('descriptors').length, 400)
  assert.equal(first.calls.some(call => call.command === 'plugin_host_scan_report'), false)
  first.dispose()
  const reopened = environment(t)
  reopened.preferences.set('pymss-plugin-library-directories-v1', JSON.stringify(['/plugins']))
  reopened.controls.cacheReport = report
  await reopened.mounts[0]()
  assert.equal(reopened.read('descriptors').length, 400)
  assert.equal(reopened.read('catalogGroups').length, 20)
  reopened.set('librarySearch', 'Effect 123')
  assert.equal(reopened.read('pluginOptions').length, 1)
  assert.equal(reopened.calls.filter(call => call.command === 'plugin_host_cached_scan_report').length, 1)
  assert.equal(reopened.calls.some(call => call.command === 'plugin_host_scan_report'), false)
  assert.equal(reopened.writes.length, 0)
})

test('late cache reads cannot overwrite a newer scan, different source or disposed editor', async t => {
  for (const action of ['scan', 'source', 'dispose']) {
    const env = environment(t)
    env.preferences.set('pymss-plugin-library-directories-v1', JSON.stringify(['/first']))
    env.controls.cacheGate = deferred()
    env.controls.cacheReport = { ...env.controls.cacheReport, plugins: [plugin('stale')], hasCache: true }
    const mounting = env.mounts[0]()
    await flush()
    if (action === 'scan') await env.call('scan')
    else if (action === 'source') {
      env.controls.cacheReport = { ...env.controls.cacheReport, plugins: [plugin('current')] }
      env.controls.cacheGate.resolve()
      env.controls.cacheGate = null
      env.set('directories', ['/second'])
    } else env.dispose()
    env.controls.cacheGate?.resolve()
    await mounting
    await flush()
    assert.equal(env.read('descriptors').some(plugin => plugin.id === 'stale'), false)
    if (action === 'scan') assert.equal(env.read('descriptors')[0].id, 'vst3:library')
    if (action === 'source') assert.equal(env.read('descriptors')[0].id, 'current')
  }
})

test('cached effects remain searchable and addable while incremental refresh waits', async t => {
  const env = environment(t)
  env.set('directories', ['/plugins'])
  env.set('descriptors', env.controls.report.plugins)
  env.controls.scanGate = deferred()
  const scanning = env.call('scan')
  await flush()
  assert.equal(env.read('scanning'), true)
  assert.equal(env.read('busy'), false)
  env.set('librarySearch', 'library')
  env.set('selectedPluginId', JSON.stringify(['vst3:library', '/library.vst3']))
  env.releaseCapture()
  await env.call('addPlugin')
  assert.equal(env.writes.length, 1)
  assert.equal(env.calls.filter(call => call.command === 'plugin_host_scan_report')[0].args.force, false)
  env.controls.scanGate.resolve()
  await scanning
})

test('stopping a refresh restores unvisited cached entries and clears feedback on project changes', async t => {
  const env = environment(t)
  env.set('capabilities', { enabled: true, formats: ['vst3'], catalogCache: true })
  env.set('directories', ['/plugins'])
  const plugins = [plugin('a'), plugin('b')]
  env.set('descriptors', plugins)
  env.controls.report = { plugins: [plugins[0]], failures: [], scanned: 0, cached: 1, cancelled: true }
  env.controls.cacheReport = { plugins, failures: [], scanned: 0, cached: 2, hasCache: true }
  await env.call('scan')
  assert.deepEqual(Array.from(env.read('descriptors'), plugin => plugin.id), ['a', 'b'])
  env.set('feedback', 'old project feedback')
  env.props.session = { ...env.props.session, id: 'new-project' }
  await flush()
  assert.equal(env.read('feedback'), '')
  assert.equal(env.read('descriptors').length, 2)
})

test('an empty committed cache after cancelled refresh removes previously deleted entries', async t => {
  const env = environment(t)
  env.set('capabilities', { enabled: true, formats: ['vst3'], catalogCache: true })
  env.set('directories', ['/plugins'])
  env.set('descriptors', [plugin('removed')])
  env.controls.report = { plugins: [], failures: [], scanned: 0, cached: 0, cancelled: true }
  await env.call('scan')
  assert.equal(env.read('descriptors').length, 0)
})

test('restoring cancelled scan cache preserves source access errors that are absent from package entries', async t => {
  const env = environment(t)
  env.set('capabilities', { enabled: true, formats: ['vst3'], catalogCache: true })
  env.set('directories', ['/offline-source'])
  env.controls.report = { plugins: [], failures: [{ path: '/offline-source', message: 'Access denied' }], scanned: 0, cached: 0, cancelled: true }
  await env.call('scan')
  assert.equal(env.read('scanReport').failures[0].message, 'Access denied')
})

test('favorites persist by plugin identity, survive relocation and share one merged VST2/VST3 row', async t => {
  const env = environment(t)
  env.set('capabilities', { enabled: true, formats: ['vst2', 'vst3'] })
  const plugins = [
    { ...plugin('gain2'), id: 'vst2:gain', name: 'Gain', vendor: 'Maker', format: 'vst2', path: '/Gain.dll' },
    { ...plugin('gain3'), id: 'vst3:gain', name: 'Gain', vendor: 'Maker', format: 'vst3', path: '/Gain.vst3' },
  ]
  env.set('descriptors', plugins)
  const row = env.read('catalogOptions')[0]
  env.call('toggleFavorite', row)
  assert.equal(env.call('isFavorite', row), true)
  const stored = env.preferences.get('pymss-plugin-favorites-v1')
  assert.equal(JSON.parse(stored).length, 2)
  assert.equal(env.writes.length, 0)
  assert.equal(env.calls.length, 0)
  env.set('catalogGroupBy', 'favorites')
  assert.equal(env.read('pluginOptions').length, 1)
  assert.equal(env.read('pluginOptions')[0].plugin.format, 'vst3')
  env.set('librarySearch', 'missing')
  assert.equal(env.read('pluginOptions').length, 0)
  env.set('librarySearch', '')
  env.dispose()
  const reopened = environment(t)
  reopened.preferences.set('pymss-plugin-favorites-v1', stored)
  await reopened.mounts[0]()
  reopened.set('capabilities', { enabled: true, formats: ['vst2', 'vst3'] })
  reopened.set('descriptors', plugins.map(plugin => ({ ...plugin, path: '/moved/' + plugin.id })))
  reopened.set('catalogGroupBy', 'favorites')
  assert.equal(reopened.read('pluginOptions').length, 1)
  reopened.call('toggleFavorite', reopened.read('catalogOptions')[0])
  assert.equal(reopened.read('pluginOptions').length, 0)
})

test('favorite edits merge the latest persisted set before a delayed storage notification', async t => {
  const env = environment(t)
  await env.mounts[0]()
  const a = pluginFavoriteKey(plugin('a')), b = pluginFavoriteKey(plugin('b'))
  env.set('descriptors', [plugin('a'), plugin('b')])
  env.preferences.set('pymss-plugin-favorites-v1', JSON.stringify([a]))
  env.call('toggleFavorite', env.read('catalogOptions')[1])
  assert.deepEqual(JSON.parse(env.preferences.get('pymss-plugin-favorites-v1')), [a, b])
  env.preferences.set('pymss-plugin-favorites-v1', JSON.stringify([b]))
  env.windowListeners.get('storage')({ key: 'pymss-plugin-favorites-v1' })
  assert.equal(env.call('isFavorite', env.read('catalogOptions')[0]), false)
  assert.equal(env.call('isFavorite', env.read('catalogOptions')[1]), true)
  env.dispose()
  assert.equal(env.windowListeners.size, 0)
})

test('failed favorite persistence retains the displayed state and does not touch project history', t => {
  const env = environment(t)
  env.set('descriptors', [plugin('a')])
  env.evaluate("localStorage.setItem = () => { throw new Error('Storage unavailable') }")
  env.call('toggleFavorite', env.read('catalogOptions')[0])
  assert.equal(env.call('isFavorite', env.read('catalogOptions')[0]), false)
  assert.match(env.read('error'), /Storage unavailable/)
  assert.equal(env.writes.length, 0)
  assert.equal(env.calls.length, 0)
})

test('Space on the manager background or a plugin row requests transport once and ignores repeats', t => {
  const env = environment(t)
  let prevented = 0
  const background = { key: ' ', target: null, preventDefault() { prevented++ } }
  env.call('onManagerKeydown', background)
  const row = { closest(selector) { return selector.startsWith('button,') ? { hasAttribute: key => key === 'data-editor-transport' } : null } }
  env.call('onPluginKeydown', { ...background, target: row })
  env.call('onPluginKeydown', { ...background, target: row, repeat: true })
  assert.equal(prevented, 3)
  assert.deepEqual(env.emitted, [{ event: 'togglePlayback', trackId: undefined }, { event: 'togglePlayback', trackId: undefined }])
})

test('Space preserves text editing, summaries, buttons and modified keys without toggling transport', t => {
  const env = environment(t)
  let prevented = 0
  const base = { key: ' ', target: null, preventDefault() { prevented++ } }
  for (const patch of [{ ctrlKey: true }, { metaKey: true }, { altKey: true }, { shiftKey: true }, { isComposing: true }, { defaultPrevented: true }, { key: 'Delete' }]) {
    env.call('onPluginKeydown', { ...base, ...patch })
  }
  for (const kind of ['input', 'summary', 'parameter', 'button', 'editable']) {
    const target = { isContentEditable: kind === 'editable', closest(selector) {
      if (selector.startsWith('input,')) return ['input', 'summary', 'parameter'].includes(kind) ? {} : null
      return kind === 'button' ? { hasAttribute: () => false } : null
    } }
    env.call('onPluginKeydown', { ...base, target })
  }
  assert.equal(prevented, 0)
  assert.equal(env.emitted.length, 0)
})
