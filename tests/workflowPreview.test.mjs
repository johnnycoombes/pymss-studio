import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import test, { after } from 'node:test'
import { createServer } from 'vite'

const vite = await createServer({
  configFile: false,
  server: { watch: null, middlewareMode: true, hmr: false, preTransformRequests: false },
  appType: 'custom', optimizeDeps: { noDiscovery: true },
  resolve: { alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) } },
})
after(() => vite.close())
const { buildWorkflowPreview } = await vite.ssrLoadModule('/src/workflows/preview.ts')
const t = key => key

test('simple preview represents the actual selected stems and processing dependencies', () => {
  const definition = {
    version: 1,
    steps: [{ id: 'vocals', model: 'voice.ckpt', input: 'input', stems: ['Vocals', 'Instrumental'], save: { Vocals: 'Default', Instrumental: false } }],
    ensembles: [{ id: 'phase', algorithm: 'invert', inputs: [{ source: 'VOCALS.INSTRUMENTAL', weight: 1 }], output_stem: 'Inverted', save: 'Default' }],
  }
  const before = JSON.stringify(definition)
  const graph = buildWorkflowPreview(definition, t)
  assert.equal(graph.nodes.length, 4)
  assert.equal(graph.links.length, 4)
  assert.deepEqual(graph.links.map(link => [link.source, link.target, link.sourceSlot, link.targetSlot]), [
    ['input', 'step:vocals', 0, 0], ['step:vocals', 'ensemble:phase', 1, 0],
    ['step:vocals', 'save', 0, 0], ['ensemble:phase', 'save', 0, 1],
  ])
  assert.equal(graph.nodes.find(node => node.id === 'step:vocals').title, 'voice.ckpt')
  assert.ok(graph.nodes.find(node => node.id === 'save').x > graph.nodes.find(node => node.id === 'ensemble:phase').x)
  assert.equal(JSON.stringify(definition), before)
})

test('simple references trim step ids and stems consistently for processing and saved outputs', () => {
  const definition = {
    version: 1,
    steps: [
      { id: ' first ', model: 'voice.ckpt', input: 'input', stems: [' Vocals '], save: { Vocals: 'Default' } },
      { id: 'last', model: 'voice.ckpt', input: ' blend.Mixed ', stems: ['Final'], save: { ' Final ': 'Default' } },
    ],
    ensembles: [{ id: ' blend ', algorithm: 'invert', inputs: [{ source: ' FIRST.VOCALS ', weight: 1 }], output_stem: ' Mixed ', save: 'Default' }],
  }
  const before = JSON.stringify(definition)
  const graph = buildWorkflowPreview(definition, t)
  assert.deepEqual(graph.links.map(link => [link.source, link.target]), [
    ['input', 'step: first '], ['ensemble: blend ', 'step:last'], ['step: first ', 'ensemble: blend '],
    ['step: first ', 'save'], ['step:last', 'save'], ['ensemble: blend ', 'save'],
  ])
  assert.equal(graph.nodes.find(node => node.id === 'save').detail, 'Vocals · Final · Mixed')
  assert.equal(JSON.stringify(definition), before)
})

test('processing-only workflows and saved simple canvas positions remain visible', () => {
  const graph = buildWorkflowPreview({
    version: 1, steps: [],
    ensembles: [{ id: 'phase', algorithm: 'invert', inputs: [{ source: 'input' }], output_stem: 'Inverted', save: 'Default' }],
    studio: { nodes: { input: { x: -200, y: 40 }, phase: { x: 160, y: 10 }, save: { x: 720, y: -80 } } },
  }, t)
  assert.equal(graph.nodes.length, 3)
  assert.equal(graph.links.length, 2)
  assert.deepEqual(graph.nodes.map(({ x, y }) => [x, y]), [[-200, 40], [160, 10], [720, -80]])
})

test('graph previews accept supported serialized link formats and preserve positions', () => {
  const graph = buildWorkflowPreview({
    nodes: [
      { id: 1, type: 'input_audio', pos: [-400, 10] },
      { id: 2, type: 'mss_separate', pos: { 0: 0, 1: 30 }, widgets_values: ['model.ckpt'] },
      { id: 3, type: 'pymss_save_audio', pos: [520, 40] },
    ],
    links: [[1, 1, 0, 2, 0, 'AUDIO'], { id: 2, origin_id: 2, origin_slot: 2, target_id: 3, target_slot: 0, type: 'AUDIO' }],
  }, t)
  assert.equal(graph.links.length, 2)
  assert.equal(graph.nodes[1].detail, 'model.ckpt')
  assert.deepEqual(graph.nodes.map(({ x, y }) => [x, y]), [[-400, 10], [0, 30], [520, 40]])
  assert.equal(graph.nodes[1].outputs, 3)
})

test('incomplete imported graphs do not invent dangling links or loop during layout', () => {
  const graph = buildWorkflowPreview({
    nodes: [{ id: 1, type: 'CustomA', title: 'Custom processor' }, { id: 2, type: 'CustomB' }],
    links: [[1, 1, 0, 2, 0, 'AUDIO'], [2, 2, 0, 1, 0, 'AUDIO'], [3, 99, 0, 2, 0, 'AUDIO'], [4, 1, -1, 2, 0, 'AUDIO']],
  }, t)
  assert.equal(graph.links.length, 2)
  assert.equal(graph.nodes[0].title, 'Custom processor')
  assert.ok(graph.nodes.every(node => Number.isFinite(node.x) && Number.isFinite(node.y)))
  assert.notEqual(graph.nodes[0].y, graph.nodes[1].y)
})

test('unknown node types matching Object prototype names remain ordinary preview nodes', () => {
  const types = ['constructor', 'toString', '__proto__', 'hasOwnProperty']
  const definition = { nodes: types.map((type, index) => ({ id: index + 1, type })), links: [] }
  const graph = buildWorkflowPreview(definition, t)
  assert.deepEqual(graph.nodes.map(node => node.title), types)
  assert.deepEqual(graph.nodes.map(node => node.detail), types)
  assert.equal(graph.links.length, 0)
})

test('empty and unrecognized definitions have an explicit empty preview', () => {
  for (const definition of [{}, { version: 1, steps: [] }, { nodes: [], links: [] }]) {
    assert.deepEqual(buildWorkflowPreview(definition, t), { nodes: [], links: [] })
  }
})

const fixtures = new URL('./fixtures/comfy-mss/', import.meta.url)
for (const file of readdirSync(fixtures).filter(file => file.endsWith('.json'))) {
  test(`preview projects ${file} without modifying its definition`, () => {
    const definition = JSON.parse(readFileSync(new URL(file, fixtures), 'utf8'))
    const before = JSON.stringify(definition)
    const graph = buildWorkflowPreview(definition, t)
    assert.equal(graph.nodes.length, definition.nodes.length)
    assert.equal(graph.links.length, definition.links.length)
    assert.equal(JSON.stringify(definition), before)
  })
}
