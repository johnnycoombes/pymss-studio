import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { parse } from 'vue/compiler-sfc'

const descriptor = parse(readFileSync(new URL('../src/views/ToolsView.vue', import.meta.url), 'utf8')).descriptor
const source = ts.createSourceFile('tools.ts', descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true)
const names = new Set(['applyRequestedTool', 'applyRequestedPluginView', 'selectTool', 'selectPluginTool'])
const functions = source.statements.filter(node => ts.isFunctionDeclaration(node) && names.has(node.name?.text))
const code = ts.transpileModule(functions.map(node => node.getText(source)).join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText

function selection(query) {
  const requests = []
  const state = {
    route: { query }, activeTool: { value: 'convert' }, activePluginTool: { value: 'gain/editor' },
    visibleAudioTools: { value: [{ id: 'convert' }, { id: 'inspect' }] },
    pluginToolViews: { value: [{ pluginName: 'gain', view: { id: 'editor' } }] },
    guide: { phase: 'paused' },
    router: { replace: value => { requests.push(JSON.parse(JSON.stringify(value))); return Promise.resolve() } },
  }
  const controller = vm.runInNewContext(`${code}\n({ applyRequestedTool, applyRequestedPluginView, selectTool, selectPluginTool })`, state)
  return { state, requests, controller }
}

test('a valid built-in tool query leaves the plugin view', () => {
  const { state, controller } = selection({ tool: 'inspect' })
  controller.applyRequestedTool()
  assert.equal(state.activePluginTool.value, '')
  assert.equal(state.activeTool.value, 'inspect')
})

test('selecting the existing built-in tool removes the plugin query', () => {
  const { state, requests, controller } = selection({ pluginView: 'gain/editor', keep: 'value' })
  controller.selectTool('convert')
  assert.equal(state.activePluginTool.value, '')
  assert.deepEqual(requests, [{ query: { tool: 'convert', keep: 'value' } }])
})

test('selecting a plugin tool removes a stale built-in query', () => {
  const { state, requests, controller } = selection({ tool: 'convert' })
  controller.selectPluginTool('gain/editor')
  assert.equal(state.activePluginTool.value, 'gain/editor')
  assert.deepEqual(requests, [{ query: { pluginView: 'gain/editor' } }])
})

test('removing or invalidating the plugin query clears the plugin view', () => {
  const { state, controller } = selection({ pluginView: 'gain/editor' })
  controller.applyRequestedPluginView()
  assert.equal(state.activePluginTool.value, 'gain/editor')
  state.route.query = {}
  controller.applyRequestedPluginView()
  assert.equal(state.activePluginTool.value, '')
  state.activePluginTool.value = 'gain/editor'
  state.route.query = { pluginView: 'missing/editor' }
  controller.applyRequestedPluginView()
  assert.equal(state.activePluginTool.value, '')
})
