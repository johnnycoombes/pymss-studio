import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { computed, effectScope, nextTick, ref, watch } from 'vue'

const source = readFileSync(new URL('../src/composables/usePluginManagerWindow.ts', import.meta.url), 'utf8')
const code = ts.transpileModule(source.replace(/^import .*$/m, '').replace('export function', 'function'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None },
}).outputText
const key = 'pymss-plugin-manager-window-v1'

async function environment(t, stored = null) {
  const mounts = [], disposals = [], writes = [], listeners = new Map(), capture = new Set()
  const storage = new Map([[key, JSON.stringify(stored)]])
  const window = { innerWidth: 1400, innerHeight: 900,
    addEventListener(name, callback) { listeners.set(name, callback) },
    removeEventListener(name) { listeners.delete(name) },
  }
  const context = vm.createContext({ computed, ref, watch, window,
    onMounted: callback => mounts.push(callback), onScopeDispose: callback => disposals.push(callback),
    localStorage: { getItem: name => storage.get(name),
      setItem(name, value) { storage.set(name, value); writes.push(JSON.parse(value)) },
      removeItem(name) { storage.delete(name); writes.push(null) } },
  })
  let layout
  const element = { style: { left: '', top: '' }, getBoundingClientRect() {
    const width = parseFloat(layout.windowStyle.value['--plugin-manager-width'])
    const height = parseFloat(layout.windowStyle.value['--plugin-manager-height'])
    const left = (window.innerWidth - width) / 2 + (parseFloat(this.style.left) || 0)
    const top = (window.innerHeight <= 640 ? 80 : 96) + (parseFloat(this.style.top) || 0)
    return { left, top, width, height, right: left + width, bottom: top + height }
  } }
  const scope = effectScope(), open = ref(true), card = ref({ $el: element })
  const create = vm.runInContext(`${code}\nusePluginManagerWindow`, context)
  layout = scope.run(() => create(open, card))
  mounts.forEach(callback => callback())
  await nextTick()
  let disposed = false
  const dispose = () => { if (!disposed) { disposed = true; disposals.forEach(callback => callback()); scope.stop() } }
  t.after(dispose)
  const handle = { focus() {}, setPointerCapture: id => capture.add(id), hasPointerCapture: id => capture.has(id), releasePointerCapture: id => capture.delete(id) }
  const pointer = (changes = {}) => ({ button: 0, pointerId: 1, clientX: 400, clientY: 400, currentTarget: handle,
    preventDefault() {}, stopPropagation() {}, ...changes })
  return { layout, open, card, writes, storage, listeners, capture, window, dispose,
    rect: () => element.getBoundingClientRect(),
    start(corner) { layout.startWindowResize(corner, pointer()) },
    emit(name, changes) { listeners.get(name)?.(pointer(changes)) },
    keyboard(corner, key) {
      const event = { ...pointer(), key, prevented: false, stopped: false,
        preventDefault() { this.prevented = true }, stopPropagation() { this.stopped = true } }
      layout.resizeWindowWithKeyboard(corner, event)
      return event
    },
  }
}

for (const corner of ['nw', 'ne', 'sw', 'se']) {
  test(`${corner} resizes both axes while keeping the opposite corner fixed`, async t => {
    const env = await environment(t)
    const before = env.rect()
    env.start(corner)
    env.emit('pointermove', { clientX: 400 + (corner.endsWith('w') ? 80 : -80), clientY: 400 + (corner.startsWith('n') ? 50 : -50) })
    const after = env.rect()
    assert.equal(after.width, before.width - 80)
    assert.equal(after.height, before.height - 50)
    assert.equal(after[corner.endsWith('w') ? 'right' : 'left'], before[corner.endsWith('w') ? 'right' : 'left'])
    assert.equal(after[corner.startsWith('n') ? 'bottom' : 'top'], before[corner.startsWith('n') ? 'bottom' : 'top'])
    assert.equal(env.writes.length, 0)
    env.emit('pointerup')
    assert.deepEqual(env.writes, [{ width: after.width, height: after.height }])
    assert.equal(env.capture.size, 0)
    assert.deepEqual([...env.listeners.keys()], ['resize'])
  })
}

test('corner bounds retain minimum size and remain on screen after an earlier header drag', async t => {
  const env = await environment(t, { width: 900, height: 600 })
  env.card.value.$el.style.left = '80px'
  env.card.value.$el.style.top = '-30px'
  const before = env.rect()
  env.start('nw')
  env.emit('pointermove', { clientX: 4000, clientY: 4000 })
  assert.equal(env.rect().width, 680)
  assert.equal(env.rect().height, 480)
  assert.equal(env.rect().right, before.right)
  assert.equal(env.rect().bottom, before.bottom)
  env.emit('pointermove', { clientX: -4000, clientY: -4000 })
  assert.equal(env.rect().left, 0)
  assert.equal(env.rect().top, 0)
  env.emit('pointerup')
  env.start('se')
  env.emit('pointermove', { clientX: 4000, clientY: 4000 })
  assert.equal(env.rect().right, env.window.innerWidth)
  assert.equal(env.rect().bottom, env.window.innerHeight)
})

test('viewport changes and reopening preserve sizes without persisting a temporary clamp', async t => {
  const env = await environment(t, { width: 1200, height: 700 })
  env.window.innerWidth = 980
  env.window.innerHeight = 680
  env.emit('resize')
  assert.equal(env.rect().width, 980)
  assert.equal(env.rect().height, 680)
  assert.equal(env.rect().top, 0)
  env.open.value = false
  await nextTick()
  env.open.value = true
  await nextTick()
  assert.equal(env.rect().top, 0)
  env.window.innerWidth = 1400
  env.window.innerHeight = 900
  env.emit('resize')
  assert.equal(env.rect().width, 1200)
  assert.equal(env.rect().height, 700)
  assert.equal(env.writes.length, 0)
})

test('Escape and pointer cancellation restore dimensions and position while keyboard resizing persists', async t => {
  const env = await environment(t)
  const initial = env.rect()
  for (const cancel of ['Escape', 'pointercancel']) {
    env.start('nw')
    env.emit('pointermove', { pointerId: 2, clientX: 600, clientY: 600 })
    assert.deepEqual(env.rect(), initial)
    env.emit('pointermove', { clientX: 600, clientY: 600 })
    if (cancel === 'Escape') assert.equal(env.keyboard('nw', cancel).stopped, true)
    else env.emit(cancel)
    assert.deepEqual(env.rect(), initial)
    assert.equal(env.layout.resizingWindow.value, null)
    assert.equal(env.capture.size, 0)
  }
  assert.equal(env.writes.length, 0)
  assert.equal(env.keyboard('se', 'Escape').stopped, false)
  env.keyboard('se', 'ArrowLeft')
  assert.equal(env.rect().width, initial.width - 16)
  assert.equal(env.writes.length, 1)
  env.layout.resetWindowSize()
  assert.deepEqual(env.rect(), initial)
  assert.equal(env.storage.has(key), false)
})

test('closing or disposing during a gesture releases listeners and capture', async t => {
  const env = await environment(t)
  env.start('se')
  env.emit('pointermove', { clientX: 380 })
  env.open.value = false
  await nextTick()
  assert.equal(env.layout.resizingWindow.value, null)
  assert.equal(env.capture.size, 0)
  env.open.value = true
  await nextTick()
  env.start('nw')
  env.dispose()
  assert.equal(env.listeners.size, 0)
  assert.equal(env.capture.size, 0)
})
