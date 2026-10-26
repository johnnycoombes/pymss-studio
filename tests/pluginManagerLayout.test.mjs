import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { computed, effectScope, nextTick, ref, watch } from 'vue'

const source = readFileSync(new URL('../src/composables/usePluginManagerLayout.ts', import.meta.url), 'utf8')
const code = ts.transpileModule(source.replace(/^import .*$/m, '').replace('export function', 'function'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None },
}).outputText
const key = 'pymss-plugin-manager-layout-v1'

async function environment(t, stored) {
  const mounts = [], disposals = [], writes = [], observers = [], listeners = new Map()
  const storage = new Map(stored === undefined ? [] : [[key, JSON.stringify(stored)]])
  const window = { innerWidth: 1400, innerHeight: 900,
    addEventListener(name, callback) { listeners.set(name, callback) },
    removeEventListener(name, callback) { if (listeners.get(name) === callback) listeners.delete(name) },
  }
  class Observer {
    constructor(callback) { this.callback = callback; observers.push(this) }
    observe() { this.disconnected = false }
    disconnect() { this.disconnected = true }
  }
  const context = vm.createContext({ computed, ref, watch, window,
    onMounted: callback => mounts.push(callback), onScopeDispose: callback => disposals.push(callback),
    ResizeObserver: Observer, MutationObserver: Observer,
    getComputedStyle: () => ({ paddingTop: '12px', paddingBottom: '12px', rowGap: '9px' }),
    localStorage: { getItem: name => storage.get(name), setItem(name, value) { storage.set(name, value); writes.push(JSON.parse(value)) } },
  })
  const scope = effectScope(), open = ref(true), parameters = ref(true)
  const create = vm.runInContext(`${code}\nusePluginManagerLayout`, context)
  const layout = scope.run(() => create(open, parameters))
  layout.managerBody.value = { clientWidth: 1000 }
  layout.managerChain.value = { clientHeight: 600,
    children: [false, true, true, true].map(excluded => ({ offsetHeight: 28, matches: () => excluded })) }
  mounts.forEach(callback => callback())
  await nextTick()
  let disposed = false
  const dispose = () => { if (!disposed) { disposed = true; disposals.forEach(callback => callback()); scope.stop() } }
  t.after(dispose)
  const captured = new Set()
  const element = { focus() {}, setPointerCapture: id => captured.add(id), hasPointerCapture: id => captured.has(id), releasePointerCapture: id => captured.delete(id) }
  function pointer(overrides = {}) {
    return { button: 0, pointerId: 1, clientX: 400, clientY: 400, currentTarget: element,
      preventDefault() {}, stopPropagation() {}, ...overrides }
  }
  function keyboard(name, shiftKey = false) {
    return { key: name, shiftKey, prevented: false, stopped: false,
      preventDefault() { this.prevented = true }, stopPropagation() { this.stopped = true } }
  }
  return { layout, open, parameters, writes, storage, listeners, observers, window, dispose, captured, keyboard,
    start(kind, overrides) { layout.startResize(kind, pointer(overrides)) },
    emit(name, overrides) { listeners.get(name)?.(pointer(overrides)) },
    resize(width, height = 600) {
      layout.managerBody.value.clientWidth = width
      layout.managerChain.value.clientHeight = height
      observers.filter(observer => !observer.disconnected).forEach(observer => observer.callback())
    },
  }
}

test('two dividers resize independently, ignore other pointers and save only on release', async t => {
  const env = await environment(t)
  const initialParameters = env.layout.parameterHeight.value
  env.start('library')
  env.emit('pointermove', { pointerId: 2, clientX: 480 })
  env.emit('pointerup', { pointerId: 2 })
  assert.equal(env.layout.libraryWidth.value, 300)
  env.emit('pointermove', { clientX: 480 })
  assert.equal(env.layout.libraryWidth.value, 380)
  assert.equal(env.layout.parameterHeight.value, initialParameters)
  assert.equal(env.writes.length, 0)
  env.emit('pointerup')
  assert.deepEqual(env.writes, [{ libraryWidth: 380 }])
  env.start('parameters')
  env.emit('pointermove', { clientY: 360 })
  assert.equal(env.layout.parameterHeight.value, initialParameters + 40)
  env.emit('pointerup')
  assert.equal(env.writes.at(-1).parameterHeight, initialParameters + 40)
  assert.equal(env.captured.size, 0)
  assert.deepEqual([...env.listeners.keys()], ['resize'])
})

test('responsive clamps retain user preferences and double click resets the chosen split', async t => {
  const env = await environment(t, { libraryWidth: 600, parameterHeight: 350 })
  env.resize(700, 360)
  assert.equal(env.layout.libraryWidth.value, 372)
  assert.equal(env.layout.parameterHeight.value, env.layout.parameterMax.value)
  assert.ok(env.layout.parameterHeight.value >= env.layout.parameterMin.value)
  env.resize(1000, 600)
  assert.equal(env.layout.libraryWidth.value, 600)
  assert.equal(env.layout.parameterHeight.value, 350)
  assert.equal(env.writes.length, 0)
  env.layout.resetSize('library')
  assert.equal(env.layout.libraryWidth.value, 300)
  assert.deepEqual(env.writes.at(-1), { parameterHeight: 350 })
  env.layout.resetSize('parameters')
  assert.deepEqual(env.writes.at(-1), {})
})

test('a short chain keeps usable parameter controls and restores the preferred height when expanded', async t => {
  const env = await environment(t, { parameterHeight: 350 })
  env.resize(700, 260)
  assert.equal(env.layout.parameterMin.value, 150)
  assert.equal(env.layout.parameterHeight.value, 150)
  assert.equal(env.layout.parameterMax.value, 150)
  assert.equal(env.writes.length, 0)
  env.resize(1000, 600)
  assert.equal(env.layout.parameterHeight.value, 350)
  assert.equal(env.writes.length, 0)
})

test('keyboard arrows, Shift and bounds work while Escape cancels a drag without closing', async t => {
  const env = await environment(t)
  env.layout.resizeWithKeyboard('library', env.keyboard('ArrowRight', true))
  assert.equal(env.layout.libraryWidth.value, 348)
  env.layout.resizeWithKeyboard('library', env.keyboard('Home'))
  assert.equal(env.layout.libraryWidth.value, env.layout.libraryMin.value)
  env.layout.resizeWithKeyboard('library', env.keyboard('End'))
  assert.equal(env.layout.libraryWidth.value, env.layout.libraryMax.value)
  env.layout.resizeWithKeyboard('parameters', env.keyboard('Home'))
  env.layout.resizeWithKeyboard('parameters', env.keyboard('ArrowUp'))
  assert.equal(env.layout.parameterHeight.value, env.layout.parameterMin.value + 16)
  const before = env.layout.parameterHeight.value, writes = env.writes.length
  env.start('parameters')
  env.emit('pointermove', { clientY: 200 })
  const escape = env.keyboard('Escape')
  env.layout.resizeWithKeyboard('parameters', escape)
  assert.equal(escape.stopped, true)
  assert.equal(escape.prevented, true)
  assert.equal(env.open.value, true)
  assert.equal(env.layout.parameterHeight.value, before)
  assert.equal(env.writes.length, writes)
  const idleEscape = env.keyboard('Escape')
  env.layout.resizeWithKeyboard('parameters', idleEscape)
  assert.equal(idleEscape.stopped, false)
})

for (const kind of ['library', 'parameters']) {
  test(`${kind} mixed pointer and keyboard resizing rolls back without changing saved preferences`, async t => {
    const stored = { libraryWidth: 340, parameterHeight: 240 }
    const env = await environment(t, stored)
    const size = kind === 'library' ? env.layout.libraryWidth : env.layout.parameterHeight
    const before = size.value
    env.start(kind)
    env.emit('pointermove', kind === 'library' ? { clientX: 440 } : { clientY: 360 })
    env.layout.resizeWithKeyboard(kind, env.keyboard(kind === 'library' ? 'ArrowRight' : 'ArrowUp'))
    assert.equal(size.value, before + 56)
    env.layout.resizeWithKeyboard(kind, env.keyboard('Home'))
    env.layout.resizeWithKeyboard(kind, env.keyboard('End'))
    assert.equal(env.writes.length, 0)
    const escape = env.keyboard('Escape')
    env.layout.resizeWithKeyboard(kind, escape)
    env.emit('pointerup')
    assert.equal(escape.stopped, true)
    assert.equal(size.value, before)
    assert.equal(env.writes.length, 0)
    assert.deepEqual(JSON.parse(env.storage.get(key)), stored)
    assert.equal(env.captured.size, 0)
    assert.equal(env.layout.resizing.value, null)
  })

  test(`${kind} pointer movement retains keyboard changes and saves once on release`, async t => {
    const env = await environment(t)
    const size = kind === 'library' ? env.layout.libraryWidth : env.layout.parameterHeight
    const before = size.value
    env.start(kind)
    env.emit('pointermove', kind === 'library' ? { clientX: 440 } : { clientY: 360 })
    env.layout.resizeWithKeyboard(kind, env.keyboard(kind === 'library' ? 'ArrowRight' : 'ArrowUp'))
    env.emit('pointermove', kind === 'library' ? { clientX: 460 } : { clientY: 340 })
    assert.equal(size.value, before + 76)
    assert.equal(env.writes.length, 0)
    env.emit('pointerup')
    assert.equal(env.writes.length, 1)
    assert.equal(env.writes[0][kind === 'library' ? 'libraryWidth' : 'parameterHeight'], before + 76)
    env.start(kind)
    env.emit('pointermove', kind === 'library' ? { clientX: 4000 } : { clientY: -4000 })
    env.layout.resizeWithKeyboard(kind, env.keyboard('Home'))
    const minimum = size.value
    env.emit('pointermove', kind === 'library' ? { clientX: 4001 } : { clientY: -4001 })
    assert.equal(size.value, minimum + 1)
    assert.equal(env.writes.length, 1)
    env.emit('pointerup')
    assert.equal(env.writes.length, 2)
  })
}

test('keyboard resizing another divider finishes the existing pointer gesture first', async t => {
  const env = await environment(t)
  const before = env.layout.parameterHeight.value
  env.start('library')
  env.emit('pointermove', { clientX: 440 })
  env.layout.resizeWithKeyboard('parameters', env.keyboard('ArrowUp'))
  assert.equal(env.layout.resizing.value, null)
  assert.equal(env.captured.size, 0)
  assert.equal(env.writes.length, 2)
  assert.deepEqual(env.writes.at(-1), { libraryWidth: 340, parameterHeight: before + 16 })
  env.emit('pointermove', { clientX: 500 })
  assert.equal(env.layout.libraryWidth.value, 340)
})

test('clicking without movement does not freeze responsive defaults and pointer cancel rolls back', async t => {
  const env = await environment(t)
  env.start('library')
  env.emit('pointermove')
  env.emit('pointerup')
  assert.equal(env.writes.length, 0)
  env.resize(1200)
  assert.equal(env.layout.libraryWidth.value, 360)
  env.start('library')
  env.emit('pointermove', { clientX: 500 })
  env.emit('pointercancel')
  assert.equal(env.layout.libraryWidth.value, 360)
  assert.equal(env.writes.length, 0)
})

test('closing either pane and disposing removes gesture listeners and disconnects observers', async t => {
  const env = await environment(t)
  env.start('parameters')
  env.emit('pointermove', { clientY: 380 })
  env.parameters.value = false
  await nextTick()
  assert.equal(env.layout.resizing.value, null)
  assert.equal(env.captured.size, 0)
  env.start('library')
  env.open.value = false
  await nextTick()
  assert.deepEqual([...env.listeners.keys()], ['resize'])
  assert.ok(env.observers.every(observer => observer.disconnected))
  env.open.value = true
  await nextTick()
  env.start('library')
  env.dispose()
  assert.equal(env.listeners.size, 0)
  assert.equal(env.captured.size, 0)
  assert.ok(env.observers.every(observer => observer.disconnected))
})

test('invalid preferences fall back safely and a stacked layout ends a horizontal drag', async t => {
  const env = await environment(t, { libraryWidth: '500', parameterHeight: -300 })
  assert.equal(env.layout.libraryWidth.value, 300)
  assert.ok(env.layout.parameterHeight.value > 0)
  env.start('library')
  env.window.innerWidth = 740
  env.resize(700)
  assert.equal(env.layout.stacked.value, true)
  assert.equal(env.layout.resizing.value, null)
  env.start('library')
  assert.equal(env.layout.resizing.value, null)
})
