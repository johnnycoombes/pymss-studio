import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { computed, ref } from 'vue'
import { calculateEditorPanelCapacity, resolveEditorPanelLayout } from '../src/utils/editorLayout.ts'

const text = readFileSync(new URL('../src/composables/useEditorLayout.ts', import.meta.url), 'utf8')
const source = ts.createSourceFile('layout.ts', text, ts.ScriptTarget.Latest, true)
const code = ts.transpileModule(source.statements.filter(node => !ts.isImportDeclaration(node))
  .map(node => node.getText(source)).join('\n').replace('export function useEditorLayout', 'function useEditorLayout'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None },
}).outputText
const flush = async () => { await new Promise(resolve => setImmediate(resolve)) }

async function environment(t, stored = {}) {
  const shell = ref({ clientWidth: 1440 })
  const mounts = [], unmounts = [], writes = [], listeners = new Map(), timers = new Map(), observers = []
  let timerId = 0
  const browserWindow = { innerWidth: 1440,
    addEventListener(name, callback) { listeners.set(name, callback) },
    removeEventListener(name) { listeners.delete(name) },
  }
  class ResizeObserver {
    constructor(callback) { this.callback = callback; observers.push(this) }
    observe(element) { this.element = element }
    disconnect() { this.disconnected = true }
  }
  const context = vm.createContext({ computed, ref, calculateEditorPanelCapacity, resolveEditorPanelLayout,
    onMounted: callback => mounts.push(callback), onBeforeUnmount: callback => unmounts.push(callback),
    window: browserWindow, ResizeObserver, loadAppStore: async () => stored,
    saveAppStore: async (name, data) => writes.push({ name, data: JSON.parse(JSON.stringify(data)) }),
    setTimeout(callback) { timers.set(++timerId, callback); return timerId },
    clearTimeout(id) { timers.delete(id) },
  })
  const create = vm.runInContext(`${code}\nuseEditorLayout`, context)
  const layout = create({ shellEl: shell, assetRailWidth: 34, inspectorRailWidth: 34, resizerWidth: 10,
    minAssetWidth: 180, maxAssetWidth: 320, minCenterWidth: 520, minInspectorWidth: 240, maxInspectorWidth: 340,
    initialAssetWidth: 218, initialInspectorWidth: 268 })
  for (const callback of mounts) callback()
  await flush()
  let disposed = false
  const dispose = () => { if (!disposed) { disposed = true; unmounts.forEach(callback => callback()) } }
  t.after(dispose)
  return { layout, writes, timers, observers, listeners, shell, browserWindow, dispose,
    resize(width) { shell.value.clientWidth = width; observers[0].callback() },
    drain() { const pending = [...timers.values()]; timers.clear(); pending.forEach(callback => callback()) },
  }
}

test('container resizing restores preferred widths and does not persist automatic hiding', async t => {
  const env = await environment(t, { assetCollapsed: false, inspectorCollapsed: false,
    assetPanelWidth: 320, inspectorPanelWidth: 340 })
  env.resize(980)
  assert.equal(env.layout.assetPanelVisible.value, false)
  assert.equal(env.layout.inspectorPanelVisible.value, true)
  assert.equal(env.layout.isAssetCollapsed.value, false)
  env.resize(1024)
  assert.equal(env.layout.assetPanelWidth.value, 180)
  assert.equal(env.layout.inspectorPanelWidth.value, 270)
  env.resize(1920)
  assert.equal(env.layout.assetPanelWidth.value, 320)
  assert.equal(env.layout.inspectorPanelWidth.value, 340)
  assert.equal(env.writes.length, 0)
  assert.equal(env.timers.size, 0)
  assert.equal(env.browserWindow.innerWidth, 1440, 'container observation must work without a window resize')
})

test('a temporarily hidden launcher opens that panel without discarding the other preference', async t => {
  const env = await environment(t, { assetCollapsed: false, inspectorCollapsed: false })
  env.resize(980)
  env.layout.toggleAssetPanel()
  assert.equal(env.layout.assetPanelVisible.value, true)
  assert.equal(env.layout.inspectorPanelVisible.value, false)
  env.drain()
  assert.equal(env.writes.at(-1).data.panelPriority, 'assets')
  assert.equal(env.writes.at(-1).data.inspectorCollapsed, false)
  env.layout.toggleInspectorPanel()
  assert.equal(env.layout.inspectorPanelVisible.value, true)
  assert.equal(env.layout.assetPanelVisible.value, false)
  env.resize(1920)
  assert.equal(env.layout.assetPanelVisible.value, true)
  assert.equal(env.layout.inspectorPanelVisible.value, true)
})

test('explicit collapsed state persists while automatic default width follows wide windows', async t => {
  const env = await environment(t, { inspectorCollapsed: false })
  assert.equal(env.layout.inspectorPanelWidth.value, 268)
  env.resize(2560)
  assert.equal(env.layout.inspectorPanelWidth.value, 340)
  env.layout.toggleInspectorPanel(false)
  env.drain()
  env.resize(980)
  env.resize(1920)
  assert.equal(env.layout.inspectorPanelVisible.value, false)
  assert.equal(env.writes.at(-1).data.inspectorCollapsed, true)
  assert.equal('inspectorPanelWidth' in env.writes.at(-1).data, false)
})

test('manual resize persists its width and unmount disconnects observers and pending timers', async t => {
  const env = await environment(t, { inspectorCollapsed: false })
  env.layout.startResize('inspector', { button: 0, clientX: 1000, preventDefault() {} })
  env.listeners.get('mousemove')({ clientX: 970 })
  assert.equal(env.layout.inspectorPanelWidth.value, 298)
  env.layout.stopResize()
  env.dispose()
  assert.equal(env.writes.at(-1).data.inspectorPanelWidth, 298)
  assert.equal(env.timers.size, 0)
  assert.equal(env.listeners.size, 0)
  assert.equal(env.observers[0].disconnected, true)
})

for (const side of ['assets', 'inspector']) {
  test(`a constrained ${side} drag can reverse while the other panel reclaims space`, async t => {
    const env = await environment(t, { inspectorCollapsed: false, assetCollapsed: false,
      inspectorPanelWidth: 340, assetPanelWidth: 320, panelPriority: side })
    env.resize(1024)
    const own = side === 'assets' ? env.layout.assetPanelWidth : env.layout.inspectorPanelWidth
    const before = own.value
    env.layout.startResize(side, { button: 0, clientX: 1000, preventDefault() {} })
    env.listeners.get('mousemove')({ clientX: side === 'assets' ? 990 : 1010 })
    assert.equal(own.value, before - 10)
    env.listeners.get('mousemove')({ clientX: 1000 })
    assert.equal(own.value, before)
    env.layout.stopResize()
  })
}
