import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { parse } from 'vue/compiler-sfc'

const filename = new URL('../src/views/EditorView.vue', import.meta.url)
const { descriptor } = parse(readFileSync(filename, 'utf8'), { filename: filename.pathname })
const script = ts.createSourceFile('EditorView.ts', descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true)
const declaration = script.statements.find(statement => ts.isFunctionDeclaration(statement) && statement.name?.text === 'handleRecordingToggle')
assert.ok(declaration, 'exercise the recording handler from the production EditorView SFC')
const code = ts.transpileModule(declaration.getText(script), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText

function environment({ importing = false, recording = false, toggleError = '' } = {}) {
  const isImportingExternal = { value: importing }
  const isRecording = { value: recording }
  const recordingState = { value: recording ? 'recording' : 'idle' }
  const recordingError = { value: '' }
  const selectedTrackId = { value: 'selected-recording-track' }
  const recordingTargetId = { value: recording ? selectedTrackId.value : null }
  const calls = []
  const notices = []
  const context = {
    isImportingExternal, isRecording, recordingState, recordingError,
    message: Object.fromEntries(['warning', 'success', 'error'].map(level => [level, text => notices.push([level, text])])),
    t: (key, values) => values?.detail ? `${key}: ${values.detail}` : key,
    async toggleRecording() {
      calls.push({ state: recordingState.value, selectedTrackId: selectedTrackId.value })
      if (toggleError) { recordingError.value = toggleError; return false }
      if (isRecording.value) {
        isRecording.value = false
        recordingState.value = 'idle'
      } else {
        recordingTargetId.value = selectedTrackId.value
        isRecording.value = true
        recordingState.value = 'recording'
      }
      return true
    },
  }
  const toggle = vm.runInNewContext(`${code}\nhandleRecordingToggle`, context)
  return { toggle, calls, notices, isImportingExternal, isRecording, recordingState, selectedTrackId, recordingTargetId }
}

test('an idle recording request during audio import leaves capture and its target untouched', async () => {
  const env = environment({ importing: true })
  await env.toggle()
  assert.deepEqual(env.calls, [])
  assert.equal(env.isRecording.value, false)
  assert.equal(env.recordingState.value, 'idle')
  assert.equal(env.recordingTargetId.value, null)
  assert.equal(env.selectedTrackId.value, 'selected-recording-track')
  assert.deepEqual(env.notices, [['warning', 'editor.recordingImportPending']])
})

test('recording can start on the selected recording track after import completes', async () => {
  const env = environment({ importing: true })
  await env.toggle()
  env.isImportingExternal.value = false
  await env.toggle()
  assert.equal(env.calls.length, 1)
  assert.equal(env.isRecording.value, true)
  assert.equal(env.recordingState.value, 'recording')
  assert.equal(env.recordingTargetId.value, 'selected-recording-track')
  assert.deepEqual(env.notices, [['warning', 'editor.recordingImportPending']])
})

test('an active recording can still stop while audio import is pending and reports its save', async () => {
  const env = environment({ importing: true, recording: true })
  await env.toggle()
  assert.equal(env.calls.length, 1)
  assert.equal(env.calls[0].state, 'recording')
  assert.equal(env.isRecording.value, false)
  assert.equal(env.recordingState.value, 'idle')
  assert.deepEqual(env.notices, [['success', 'editor.recordingSaved']])
})

test('recording failures retain their original error and do not claim a successful save', async () => {
  for (const recording of [false, true]) {
    const env = environment({ recording, importing: recording, toggleError: 'Microphone or recording storage is unavailable' })
    await env.toggle()
    assert.equal(env.calls.length, 1)
    assert.deepEqual(env.notices, [['error', 'editor.recordingFailed: Microphone or recording storage is unavailable']])
  }
})
