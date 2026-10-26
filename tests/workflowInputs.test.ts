import assert from 'node:assert/strict'
import test from 'node:test'
import { analyzeWorkflowInputs, getSeparateWorkflowInputIssue } from '../src/utils/workflowInputs.ts'

test('named load nodes expose runtime file slots', () => {
  const result = analyzeWorkflowInputs({
    nodes: [
      { id: 1, type: 'pymss_load_audio', widgets_values: ['input.wav', 'lead'] },
      { id: 2, type: 'pymss_load_audio', widgets_values: ['input.wav', 'lead'] },
    ],
  })
  assert.deepEqual(result.slots, [{ name: 'lead', nodeType: 'pymss_load_audio', nodeId: 1 }])
  assert.equal(result.selfContained, 0)
  assert.deepEqual(result.unresolved, [])
})

test('legacy load widgets become explicit runtime slot keys', () => {
  const result = analyzeWorkflowInputs({
    nodes: [{ id: 1, type: 'LoadAudio', widgets_values: ['input.wav'] }],
  })
  assert.deepEqual(result.slots, [{ name: 'input.wav', nodeType: 'LoadAudio', nodeId: 1 }])
  assert.deepEqual(result.unresolved, [])
})

test('batch folders and embedded file paths are self-contained inputs', () => {
  const result = analyzeWorkflowInputs({
    nodes: [
      { id: 1, type: 'pymss_load_audio_batch', widgets_values: ['D:/Audio', true, true, ''] },
      { id: 2, type: 'pymss_load_audio', widgets_values: ['D:/Audio/song.wav', ''] },
    ],
  })
  assert.equal(result.selfContained, 2)
  assert.deepEqual(result.slots, [])
  assert.deepEqual(result.unresolved, [])
})

test('empty load nodes remain unresolved instead of falling back silently', () => {
  const result = analyzeWorkflowInputs({
    nodes: [
      { id: 1, type: 'pymss_load_audio_batch', widgets_values: ['', false, true, ''] },
      { id: 2, type: 'pymss_load_audio', widgets_values: ['', ''] },
    ],
  })
  assert.deepEqual(result.unresolved.map(item => item.nodeId), [1, 2])
  assert.equal(result.selfContained, 0)
})

test('separation accepts one input feeding several processing branches', () => {
  assert.equal(getSeparateWorkflowInputIssue({
    nodes: [
      { id: 1, type: 'pymss_load_audio', widgets_values: ['input.wav', ''] },
      { id: 2, type: 'pymss_mss_separate' },
      { id: 3, type: 'pymss_mss_separate' },
      { id: 4, type: 'pymss_save_audio' },
    ], links: [],
  }), null)
})

test('separation counts reading nodes even when their runtime slot names match', () => {
  const definition = { nodes: [
    { id: 1, type: 'pymss_load_audio', widgets_values: ['input.wav', 'lead'] },
    { id: 2, type: 'pymss_load_audio', widgets_values: ['input.wav', 'lead'] },
  ], links: [] }
  assert.deepEqual(getSeparateWorkflowInputIssue(definition), { code: 'multiple-inputs', count: 2 })
})

test('separation counts fixed, legacy, batch, and compiled input nodes together', () => {
  for (const type of ['pymss_load_audio', 'LoadAudio', 'pymss_load_audio_batch', 'input_audio']) {
    const node = { id: 1, type, widgets_values: ['D:/Audio/song.wav', ''] }
    assert.equal(getSeparateWorkflowInputIssue({ nodes: [node], links: [] }), null, type)
    assert.deepEqual(getSeparateWorkflowInputIssue({ nodes: [node, { id: 2, type: 'input_audio' }], links: [] }),
      { code: 'multiple-inputs', count: 2 }, type)
  }
})

test('separation redirects graphs without an input to the workflow page', () => {
  assert.deepEqual(getSeparateWorkflowInputIssue({ nodes: [{ id: 1, type: 'pymss_save_audio' }], links: [] }),
    { code: 'no-input', count: 0 })
})

test('simple workflows keep one source regardless of their step count and definitions stay unchanged', () => {
  const simple = { version: 1, steps: [{ id: 'first', input: 'input' }, { id: 'second', input: 'first:Vocals' }] }
  const saved = structuredClone(simple)
  assert.equal(getSeparateWorkflowInputIssue(simple), null)
  assert.deepEqual(simple, saved)
  assert.equal(getSeparateWorkflowInputIssue({}), null)
})
