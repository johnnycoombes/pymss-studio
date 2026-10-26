import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { parse } from 'vue/compiler-sfc'

const descriptor = parse(readFileSync(new URL('../src/components/workflow/WorkflowNodeEditorLite.vue', import.meta.url), 'utf8')).descriptor
const source = ts.createSourceFile('editor.ts', descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true)
const names = new Set(['initializeGraph', 'reportLoadError', 'snapshotDefinition', 'onSave'])
const statements = source.statements.filter(node => ts.isFunctionDeclaration(node) && names.has(node.name?.text))
assert.equal(statements.length, names.size)
const code = ts.transpileModule(statements.map(node => node.getText(source)).join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText

function setup(definition, { createStarter = false, failed = false } = {}) {
  const calls = []
  const graph = {
    nodes: [], extra: {},
    start: () => calls.push('start'),
    stop: () => calls.push('stop'),
    serialize: () => ({ nodes: graph.nodes, links: [] }),
  }
  const state = {
    props: { createStarter, defaultDevice: 'auto', defaultFormat: 'wav' },
    definition: { value: definition }, graphRef: { value: graph },
    canvasRef: { value: { ds: { scale: 1, offset: [0, 0] }, stopRendering: () => calls.push('stop-rendering') } },
    ready: { value: false }, loadError: { value: '' }, graphInitialized: false, awaitingInitialDefinition: false,
    emit: (...args) => calls.push(args), t: key => key,
    isGraphWorkflowDefinition: value => Array.isArray(value.nodes),
    loadDefinition(value) {
      calls.push('load')
      graph.nodes = [{ id: 'partial' }]
      if (failed) throw new Error('Invalid node data')
      graph.nodes = value.nodes
    },
    seedStarterGraph() { calls.push('seed'); graph.nodes = [{ id: 'starter' }] },
    writeGraphDefaults: () => calls.push('defaults'), resetHistory: () => calls.push('history'),
    litegraphToComfy: value => value, studioLog: { error() {} },
  }
  const editor = vm.runInNewContext(`${code}\n({ initializeGraph, snapshotDefinition, onSave })`, state)
  return { state, calls, editor }
}

test('failed loading cannot seed, publish or save a partial graph', () => {
  const original = { nodes: [{ id: 1, type: 'pymss_save_audio' }], links: [], extra: { source: 'original' } }
  const { editor, calls, state } = setup(original, { failed: true })
  editor.initializeGraph()
  assert.equal(state.definition.value, original)
  assert.equal(editor.snapshotDefinition(), original)
  assert.equal(state.ready.value, false)
  assert.equal(state.canvasRef.value.read_only, true)
  assert.ok(calls.includes('stop-rendering'))
  assert.ok(calls.some(call => Array.isArray(call) && call[0] === 'load-error'))
  assert.ok(!calls.includes('seed') && !calls.includes('start') && !calls.includes('history'))
  editor.onSave()
  assert.ok(!calls.some(call => Array.isArray(call) && call[0] === 'save'))
})

test('an existing empty graph is loaded without inserting starter nodes', () => {
  const { editor, calls, state } = setup({ nodes: [], links: [] })
  editor.initializeGraph()
  assert.ok(calls.includes('load'))
  assert.ok(!calls.includes('seed'))
  assert.equal(state.definition.value.nodes.length, 0)
})

test('a new graph receives the starter only when explicitly requested', () => {
  const { editor, calls } = setup({ nodes: [], links: [] }, { createStarter: true })
  editor.initializeGraph()
  assert.ok(calls.includes('seed'))
  assert.ok(!calls.includes('load'))
})
