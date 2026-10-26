import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { Window } from 'happy-dom'
import { createServer, transformWithEsbuild } from 'vite'
import { parse, compileScript } from 'vue/compiler-sfc'

const browser = new Window({ url: 'http://localhost' })
for (const key of ['window', 'document', 'navigator', 'Node', 'Element', 'HTMLElement', 'SVGElement', 'Image', 'MouseEvent', 'KeyboardEvent', 'Event', 'ResizeObserver']) {
  Object.defineProperty(globalThis, key, { configurable: true, value: key === 'window' ? browser : browser[key] })
}
for (const key of ['getComputedStyle', 'requestAnimationFrame', 'cancelAnimationFrame']) {
  Object.defineProperty(globalThis, key, { configurable: true, value: browser[key].bind(browser) })
}
browser.__TAURI_INTERNALS__ = {
  async invoke(command, args) {
    assert.equal(command, 'save_editor_project')
    return args.project
  },
}
const { createApp, h, nextTick, ref } = await import('vue')
const { createPinia } = await import('pinia')
const naive = await import('naive-ui')
const NSlider = naive.NSlider || naive.default.NSlider
const vite = await createServer({
  configFile: false,
  server: { watch: null, middlewareMode: true, hmr: false, preTransformRequests: false },
  appType: 'custom',
  optimizeDeps: { noDiscovery: true },
  resolve: { alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) } },
  plugins: [{
    name: 'editor-slider-sfc',
    async transform(code, id) {
      const path = id.replaceAll('\\', '/')
      if (!['EditorParameterSlider.vue', 'EditorMasterLimiter.vue'].some(name => path.endsWith(`/src/components/editor/${name}`))) return null
      const { descriptor } = parse(code, { filename: path })
      const script = compileScript(descriptor, { id: 'editor-parameter-slider-test', inlineTemplate: true })
      return transformWithEsbuild(script.content, `${path}.ts`, { loader: 'ts', tsconfigRaw: {} })
    },
  }],
})
after(async () => { await vite.close(); await browser.happyDOM.abort() })
const Slider = (await vite.ssrLoadModule('/src/components/editor/EditorParameterSlider.vue')).default
const Limiter = (await vite.ssrLoadModule('/src/components/editor/EditorMasterLimiter.vue')).default
const { useEditorShortcuts } = await vite.ssrLoadModule('/src/composables/useEditorShortcuts.ts')
const { useEditorStore } = await vite.ssrLoadModule('/src/stores/editor.ts')
const i18n = (await vite.ssrLoadModule('/src/i18n/index.ts')).default

const parameters = [
  { name: 'master-volume', reset: 1, min: 0, max: 2, get: s => s.masterVolume, set: (s, v) => s.setMasterVolume(v) },
  { name: 'master-pan', reset: 0, min: -1, max: 1, get: s => s.masterPan, set: (s, v) => s.setMasterPan(v) },
  { name: 'track-volume', reset: 1, min: 0, max: 2, get: s => s.session.tracks[0].volume, set: (s, v) => s.setTrackVolume('track', v) },
  { name: 'track-pan', reset: 0, min: -1, max: 1, get: s => s.session.tracks[0].pan, set: (s, v) => s.setTrackPan('track', v) },
]
const flush = async () => { await nextTick(); await new Promise(resolve => setImmediate(resolve)); await nextTick() }
const resetLabel = spec => spec.reset === 1 ? '100%' : spec.min < 0 ? 'Center' : '0%'
const updatedValues = env => env.emitted.filter(([kind]) => kind === 'update').map(([, , value]) => value)

async function mountSliders(t, specs = [parameters[0]], { disabled = false, value = 0.4 } = {}) {
  const pinia = createPinia()
  const store = useEditorStore(pinia)
  store.session = {
    id: 'slider-project', name: 'Project', masterVolume: value, masterPan: value,
    sources: [], createdAt: 1, updatedAt: 1,
    tracks: [{ id: 'track', name: 'Track', role: 'stem', volume: value, pan: value,
      muted: false, solo: false, fadeIn: 0, fadeOut: 0, clips: [] }],
  }
  store.clearHistory()
  const visible = ref(true)
  const inactive = ref(disabled)
  const emitted = []
  const container = document.createElement('div')
  document.body.append(container)
  const app = createApp({
    setup: () => () => visible.value ? h('div', specs.map(spec => h(Slider, {
      key: spec.name, value: spec.get(store), resetValue: spec.reset, disabled: inactive.value,
      label: spec.name, resetLabel: resetLabel(spec),
      min: spec.min, max: spec.max, step: 0.01, tooltip: false,
      'onUpdate:value': value => { emitted.push(['update', spec.name, value]); spec.set(store, value) },
      onDragstart: () => { emitted.push(['begin', spec.name]); store.beginInteraction() },
      onDragend: () => { emitted.push(['end', spec.name]); store.commitInteraction() },
    }))) : null,
  })
  app.use(pinia).use(i18n).component('NSlider', NSlider).mount(container)
  await flush()
  for (const rail of container.querySelectorAll('.n-slider-handles')) {
    rail.getBoundingClientRect = () => ({ x: 0, y: 0, left: 0, right: 100, top: 0, bottom: 20, width: 100, height: 20 })
  }
  t.after(async () => { app.unmount(); await store.flushSave(); store.$dispose(); container.remove() })
  const slider = (index = 0) => container.querySelectorAll('.n-slider')[index]
  return { store, emitted, inactive, visible, slider,
    handle: index => slider(index).querySelector('[role="slider"]'),
    resetButton: (index = 0) => container.querySelectorAll('.editor-parameter-slider__reset')[index] }
}

async function mouse(target, type, x, detail = 1, options = {}) {
  target.dispatchEvent(new browser.MouseEvent(type, { bubbles: true, cancelable: true, button: 0, clientX: x, clientY: 10, detail, ...options }))
  await flush()
}
async function click(target, x, detail = 1) {
  await mouse(target, 'mousedown', x, detail)
  await mouse(document, 'mouseup', x, detail)
  await mouse(target, 'click', x, detail)
}
async function doubleClick(target, x) {
  await click(target, x, 1)
  await click(target, x, 2)
  await mouse(target, 'dblclick', x, 2)
}

test('limiter switch Space activation preserves playback while the editor background retains its shortcut', async t => {
  for (const initiallyPlaying of [false, true]) {
    await t.test(initiallyPlaying ? 'playing' : 'paused', async t => {
      let playing = initiallyPlaying
      let playbackToggles = 0
      const value = ref({ enabled: false })
      const updates = []
      const noop = () => {}
      const container = document.createElement('div')
      document.body.append(container)
      const app = createApp({
        setup() {
          useEditorShortcuts({
            togglePlay: () => { playing = !playing; playbackToggles++ },
            stop: noop, undo: noop, redo: noop, zoomIn: noop, zoomOut: noop, save: noop,
            toHome: noop, seek: noop, toggleMute: noop, toggleSolo: noop, removeTrack: noop,
          })
          return () => h('div', [
            h('div', { class: 'editor-background', tabindex: 0 }),
            h(Limiter, { value: value.value, onUpdate: patch => {
              updates.push(patch)
              value.value = { ...value.value, ...patch }
            } }),
          ])
        },
      })
      app.use(i18n).component('NSlider', NSlider)
        .component('NSwitch', naive.NSwitch || naive.default.NSwitch)
        .component('NInputNumber', naive.NInputNumber || naive.default.NInputNumber).mount(container)
      t.after(() => { app.unmount(); container.remove() })
      await flush()
      const toggle = container.querySelector('[role="switch"]')
      assert.ok(toggle)
      toggle.focus()
      assert.equal(document.activeElement, toggle)
      for (const repeat of [false, true]) {
        toggle.dispatchEvent(new browser.KeyboardEvent('keydown', { key: ' ', code: 'Space', repeat, bubbles: true, cancelable: true }))
      }
      assert.equal(value.value.enabled, false, 'the switch activates on keyup')
      toggle.dispatchEvent(new browser.KeyboardEvent('keyup', { key: ' ', code: 'Space', bubbles: true, cancelable: true }))
      await flush()
      assert.equal(toggle.getAttribute('aria-checked'), 'true')
      assert.deepEqual(updates, [{ enabled: true }])
      assert.equal(playbackToggles, 0, 'Space and key repeats must not reach the transport shortcut')
      assert.equal(playing, initiallyPlaying)
      toggle.click()
      await flush()
      assert.equal(value.value.enabled, false, 'mouse activation remains available')
      assert.equal(playbackToggles, 0)
      const background = container.querySelector('.editor-background')
      background.focus()
      background.dispatchEvent(new browser.KeyboardEvent('keydown', { key: ' ', code: 'Space', bubbles: true, cancelable: true }))
      await flush()
      assert.equal(playbackToggles, 1)
      assert.equal(playing, !initiallyPlaying)
      assert.equal(value.value.enabled, false)
    })
  }
})

test('Default buttons reset every mixer parameter atomically and expose the target to assistive technology', async t => {
  for (const parameter of parameters) {
    await t.test(parameter.name, async t => {
      const env = await mountSliders(t, [parameter])
      const button = env.resetButton()
      assert.ok(button, 'provide an explicit reset button')
      assert.equal(button.tagName, 'BUTTON')
      assert.equal(button.tabIndex, 0)
      assert.match(button.textContent, /Default|默认/)
      assert.ok(button.getAttribute('aria-label').includes(parameter.name))
      assert.ok(button.title.includes(resetLabel(parameter)))
      button.focus()
      assert.equal(document.activeElement, button)
      button.click()
      await flush()
      assert.equal(parameter.get(env.store), parameter.reset)
      assert.deepEqual(env.emitted.map(([kind]) => kind), ['begin', 'update', 'end'])
      assert.deepEqual(updatedValues(env), [parameter.reset])
      assert.equal(button.disabled, true)
      env.store.undo()
      assert.equal(parameter.get(env.store), 0.4)
      assert.equal(env.store.canUndo, false)
      env.store.redo()
      assert.equal(parameter.get(env.store), parameter.reset)
    })
  }
})

test('Space on the focused Default button preserves native activation without reaching global shortcuts', async t => {
  const env = await mountSliders(t)
  let globalSpaceEvents = 0
  const globalShortcut = event => {
    if (event.code !== 'Space') return
    globalSpaceEvents += 1
    event.preventDefault()
  }
  document.addEventListener('keydown', globalShortcut)
  t.after(() => document.removeEventListener('keydown', globalShortcut))
  const button = env.resetButton()
  button.focus()
  assert.equal(document.activeElement, button)
  const keydown = new browser.KeyboardEvent('keydown', { key: ' ', code: 'Space', bubbles: true, cancelable: true })
  button.dispatchEvent(keydown)
  assert.equal(globalSpaceEvents, 0)
  assert.equal(keydown.defaultPrevented, false)
  button.click()
  await flush()
  assert.equal(env.store.masterVolume, 1)
  assert.deepEqual(updatedValues(env), [1])
  assert.deepEqual(env.emitted.map(([kind]) => kind), ['begin', 'update', 'end'])
  env.store.undo()
  assert.equal(env.store.masterVolume, 0.4)
  assert.equal(env.store.canUndo, false)
})

test('double-clicking the handle edge resets without an intermediate positional update', async t => {
  const env = await mountSliders(t)
  await doubleClick(env.handle(), 23)
  assert.equal(env.store.masterVolume, 1)
  assert.deepEqual(updatedValues(env), [1])
  assert.deepEqual(env.emitted.map(([kind]) => kind), ['begin', 'update', 'end'])
  env.store.undo()
  assert.equal(env.store.masterVolume, 0.4)
  assert.equal(env.store.canUndo, false)
})

test('ordinary rail double-click adjusts the value and never resets it', async t => {
  const env = await mountSliders(t)
  await doubleClick(env.slider().querySelector('.n-slider-rail'), 85)
  assert.equal(env.store.masterVolume, 1.7)
  assert.deepEqual(updatedValues(env), [1.7])
  env.store.undo()
  assert.equal(env.store.masterVolume, 0.4)
  assert.equal(env.store.canUndo, false)
})

test('a rail click that moves the handle under the pointer does not become a handle reset', async t => {
  const env = await mountSliders(t)
  await click(env.slider().querySelector('.n-slider-rail'), 85, 1)
  await click(env.handle(), 85, 2)
  await mouse(env.handle(), 'dblclick', 85, 2)
  assert.equal(env.store.masterVolume, 1.7)
  assert.deepEqual(updatedValues(env), [1.7])
  env.store.undo()
  assert.equal(env.store.masterVolume, 0.4)
  assert.equal(env.store.canUndo, false)
})

test('Alt-left-click resets anywhere on the rail without starting Naive positional updates', async t => {
  for (const x of [5, 95]) {
    await t.test(`position ${x}`, async t => {
      const env = await mountSliders(t)
      await mouse(env.slider(), 'mousedown', x, 1, { altKey: true })
      await mouse(document, 'mousemove', 85)
      await mouse(document, 'mouseup', 85)
      assert.equal(env.store.masterVolume, 1)
      assert.deepEqual(updatedValues(env), [1])
      assert.deepEqual(env.emitted.map(([kind]) => kind), ['begin', 'update', 'end'])
      env.store.undo()
      assert.equal(env.store.masterVolume, 0.4)
      assert.equal(env.store.canUndo, false)
    })
  }
})

test('disabled sliders reject double-click, dragging and keyboard updates', async t => {
  const env = await mountSliders(t, undefined, { disabled: true })
  assert.equal(env.resetButton().disabled, true)
  env.resetButton().click()
  await doubleClick(env.handle(), 23)
  await mouse(env.slider(), 'mousedown', 85, 1, { altKey: true })
  await mouse(document, 'mousemove', 95)
  env.handle().dispatchEvent(new browser.KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }))
  await flush()
  assert.equal(env.store.masterVolume, 0.4)
  assert.deepEqual(env.emitted, [])
  assert.equal(env.store.canUndo, false)
})

test('ordinary drag groups updates while keyboard steps remain independent undo actions', async t => {
  const env = await mountSliders(t)
  assert.equal(env.handle().getAttribute('aria-valuemin'), '0')
  assert.equal(env.handle().getAttribute('aria-valuemax'), '2')
  await mouse(env.handle(), 'mousedown', 23)
  assert.equal(env.store.masterVolume, 0.4, 'pressing the thumb edge must not jump')
  await mouse(document, 'mousemove', 40)
  await mouse(document, 'mousemove', 60)
  await mouse(document, 'mouseup', 60)
  assert.equal(env.store.masterVolume, 1.2)
  env.handle().focus()
  env.handle().dispatchEvent(new browser.KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true, cancelable: true }))
  await flush()
  assert.equal(env.store.masterVolume, 1.21)
  env.store.undo()
  assert.equal(env.store.masterVolume, 1.2)
  env.store.undo()
  assert.equal(env.store.masterVolume, 0.4)
  assert.equal(env.store.canUndo, false)
})

test('neighboring sliders retain separate reset history', async t => {
  const env = await mountSliders(t, parameters.slice(0, 2))
  env.resetButton(0).click()
  await flush()
  env.resetButton(1).click()
  await flush()
  assert.equal(env.store.masterVolume, 1)
  assert.equal(env.store.masterPan, 0)
  env.store.undo()
  assert.equal(env.store.masterPan, 0.4)
  assert.equal(env.store.masterVolume, 1)
  env.store.undo()
  assert.equal(env.store.masterVolume, 0.4)
  assert.equal(env.store.canUndo, false)
})

test('default values and unchanged handle presses do not add undo history', async t => {
  const env = await mountSliders(t, undefined, { value: 1 })
  assert.equal(env.resetButton().disabled, true)
  env.resetButton().click()
  await doubleClick(env.handle(), 53)
  await mouse(env.slider(), 'mousedown', 85, 1, { altKey: true })
  await mouse(document, 'mouseup', 85)
  assert.equal(env.store.masterVolume, 1)
  assert.deepEqual(env.emitted, [])
  assert.equal(env.store.canUndo, false)
})

test('unmount closes an active interaction so later edits can start their own undo action', async t => {
  const env = await mountSliders(t)
  await mouse(env.handle(), 'mousedown', 20)
  await mouse(document, 'mousemove', 60)
  env.visible.value = false
  await flush()
  assert.equal(env.emitted.filter(([kind]) => kind === 'begin').length, 1)
  assert.equal(env.emitted.filter(([kind]) => kind === 'end').length, 1)
  env.store.beginInteraction()
  env.store.setMasterVolume(0.8)
  env.store.commitInteraction()
  env.store.undo()
  assert.equal(env.store.masterVolume, 1.2)
  env.store.undo()
  assert.equal(env.store.masterVolume, 0.4)
  assert.equal(env.store.canUndo, false)
})

test('disabling during a drag commits once and blocks further gesture updates', async t => {
  const env = await mountSliders(t)
  await mouse(env.handle(), 'mousedown', 20)
  await mouse(document, 'mousemove', 60)
  env.inactive.value = true
  await flush()
  await mouse(document, 'mousemove', 85)
  await mouse(document, 'mouseup', 85)
  assert.equal(env.store.masterVolume, 1.2)
  assert.equal(env.emitted.filter(([kind]) => kind === 'begin').length, 1)
  assert.equal(env.emitted.filter(([kind]) => kind === 'end').length, 1)
  env.inactive.value = false
  await flush()
  env.store.beginInteraction()
  env.store.setMasterVolume(0.8)
  env.store.commitInteraction()
  env.store.undo()
  assert.equal(env.store.masterVolume, 1.2)
  env.store.undo()
  assert.equal(env.store.masterVolume, 0.4)
  assert.equal(env.store.canUndo, false)
})
