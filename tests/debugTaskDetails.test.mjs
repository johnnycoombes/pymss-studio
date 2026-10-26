import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { computed, effectScope, nextTick, reactive, ref, watch } from 'vue'
import { summarizeDiagnosticChecks } from '../src/utils/diagnostics.ts'
import { normalizeLogRecord } from '../src/utils/logRecord.ts'

const source = readFileSync(new URL('../src/views/DebugView.vue', import.meta.url), 'utf8')
  .match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
const parsed = ts.createSourceFile('DebugView.vue', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
const declarationNames = [
  'diagnosticDetailsOpen', 'executionLogVisible', 'executionLogTaskId', 'executionDownloadSnapshot',
  'executionLogTask', 'liveExecutionDownload', 'executionLogDownload', 'executionLogName',
  'executionLogs', 'executionNodeLogs', 'executionLogError', 'canReturnToTask', 'diagnosticSummary',
]
const watchedNames = ['liveExecutionDownload', 'diagnosticSummary']

function containsIdentifier(node, name) {
  return ts.isIdentifier(node) && node.text === name
    || Boolean(ts.forEachChild(node, child => containsIdentifier(child, name)))
}

const declarations = declarationNames.map(name => {
  const statement = parsed.statements.find(node => ts.isVariableStatement(node)
    && node.declarationList.declarations.some(declaration => declaration.name.getText(parsed) === name))
  assert.ok(statement, `DebugView declaration ${name} must exist`)
  return statement
})
const watchers = watchedNames.map(name => {
  const statement = parsed.statements.find(node => ts.isExpressionStatement(node)
    && ts.isCallExpression(node.expression)
    && ts.isIdentifier(node.expression.expression) && node.expression.expression.text === 'watch'
    && containsIdentifier(node.expression.arguments[0], name))
  assert.ok(statement, `DebugView watcher for ${name} must exist`)
  return statement
})
const showExecutionLogs = parsed.statements.find(node => ts.isFunctionDeclaration(node)
  && node.name?.text === 'showExecutionLogs')
assert.ok(showExecutionLogs, 'DebugView showExecutionLogs must exist')
const statements = [...declarations, ...watchers, showExecutionLogs].sort((left, right) => left.pos - right.pos)
const code = ts.transpileModule(statements.map(node => node.getText(parsed)).join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText

function environment(t, { enabled = true, state = 'ok' } = {}) {
  const scope = effectScope()
  const inputs = {
    developerMode: ref(enabled),
    diagnostics: ref([{ key: 'runtime', group: 'runtime', label: 'Runtime', state, action: 'runtime' }]),
    downloadTasks: ref({}),
    modelStore: reactive({ downloadStates: {} }),
    task: reactive({ tasks: [] }),
  }
  const exposed = scope.run(() => vm.runInNewContext(
    `${code}\n({ ${declarationNames.join(', ')}, showExecutionLogs })`,
    { ...inputs, computed, ref, watch, normalizeLogRecord, summarizeDiagnosticChecks },
  ))
  t.after(() => scope.stop())
  return { ...inputs, ...exposed }
}

function download(patch = {}) {
  return {
    taskId: 'download-a', model: 'model-a', status: 'downloading', progress: 10, message: 'Started',
    completedFiles: 0, totalFiles: 1, updatedAt: 1, logs: [], speedBytesPerSecond: 256,
    ...patch,
  }
}

function messages(details) {
  return Array.from(details.executionLogs.value, entry => normalizeLogRecord(entry).message)
}

test('download snapshots follow replacement objects and new logs synchronously', t => {
  const details = environment(t)
  details.downloadTasks.value = { 'model-a': download() }
  details.showExecutionLogs('download-a')
  const opened = details.executionDownloadSnapshot.value
  const latest = download({ progress: 90, logs: [{ ts: 1700000000000, level: 'info', message: 'Latest transfer output' }] })
  details.downloadTasks.value = { 'model-a': latest }

  assert.notStrictEqual(details.executionDownloadSnapshot.value, opened)
  assert.strictEqual(details.executionDownloadSnapshot.value, details.downloadTasks.value['model-a'])
  assert.equal(details.executionLogDownload.value.progress, 90)
  assert.deepEqual(messages(details), ['Latest transfer output'])
  assert.equal(details.executionLogs.value[0].taskId, 'download-a')
  assert.equal(details.executionLogs.value[0].timestamp, '2023-11-14T22:13:20.000Z')
})

test('a failed download keeps its latest error and logs when a retry replaces the model entry', t => {
  const details = environment(t)
  details.downloadTasks.value = { 'model-a': download() }
  details.showExecutionLogs('download-a')
  details.downloadTasks.value = { 'model-a': download({
    status: 'error', errorMessage: 'Connection reset', message: 'Connection reset',
    logs: [{ ts: 1700000000000, level: 'error', message: 'Download failed after transfer' }],
  }) }
  assert.equal(details.executionLogError.value, 'Connection reset')

  details.downloadTasks.value = { 'model-a': download({ taskId: 'download-retry', progress: 0, logs: [] }) }
  details.downloadTasks.value = { 'model-a': download({ taskId: 'download-retry', progress: 75, logs: [
    { ts: 1700000001000, level: 'info', message: 'Retry output' },
  ] }) }

  assert.equal(details.liveExecutionDownload.value, undefined)
  assert.equal(details.executionLogDownload.value.taskId, 'download-a')
  assert.equal(details.executionLogDownload.value.status, 'error')
  assert.equal(details.executionLogError.value, 'Connection reset')
  assert.deepEqual(messages(details), ['Download failed after transfer'])
  assert.ok(details.canReturnToTask.value)
})

test('successful cleanup preserves the latest logs and a completed terminal state', t => {
  const details = environment(t)
  details.downloadTasks.value = { 'model-a': download() }
  details.showExecutionLogs('download-a')
  const record = normalizeLogRecord({
    schemaVersion: 1, id: 'transfer-warning', source: 'worker', level: 'warn',
    message: 'Transfer resumed', detail: 'Connection reset on an earlier connection', taskId: 'download-a',
  })
  details.downloadTasks.value = { 'model-a': download({ progress: 99, logs: [{ ts: 1, level: 'warn', message: record.message, record }] }) }
  details.modelStore.downloadStates['model-a'] = 'done'
  details.downloadTasks.value = {}

  assert.equal(details.executionLogDownload.value.status, 'done')
  assert.equal(details.executionLogDownload.value.progress, 100)
  assert.equal(details.executionLogDownload.value.speedBytesPerSecond, 0)
  assert.deepEqual(messages(details), ['Transfer resumed'])
  assert.equal(details.executionLogs.value[0].detail, 'Connection reset on an earlier connection')
  assert.equal(details.executionLogName.value, 'model-a')
})

test('switching selected IDs never carries download logs or errors into another task', t => {
  const details = environment(t)
  details.downloadTasks.value = {
    'model-a': download({ status: 'error', errorMessage: 'First download failed' }),
    'model-b': download({ taskId: 'download-b', model: 'model-b' }),
  }
  details.task.tasks = [{ id: 'separation-a', model: 'separator', status: 'failed', error: 'Separation failed', logs: ['Execution output'] }]
  details.showExecutionLogs('download-a')
  details.showExecutionLogs('download-b')
  assert.equal(details.executionDownloadSnapshot.value.taskId, 'download-b')
  assert.equal(details.executionLogName.value, 'model-b')
  assert.equal(details.executionLogError.value, '')

  details.showExecutionLogs('separation-a')
  assert.equal(details.executionDownloadSnapshot.value, null)
  assert.equal(details.executionLogDownload.value, null)
  assert.equal(details.executionLogError.value, 'Separation failed')
  assert.deepEqual(messages(details), ['Execution output'])

  details.showExecutionLogs('missing-task')
  assert.equal(details.executionDownloadSnapshot.value, null)
  assert.equal(details.executionLogDownload.value, null)
  assert.equal(details.executionLogName.value, 'missing-task')
  assert.equal(details.executionLogError.value, '')
  assert.deepEqual(messages(details), [])
  assert.equal(Boolean(details.canReturnToTask.value), false)
})

test('reopening a removed download preserves its snapshot only for the same ID', t => {
  const details = environment(t)
  details.downloadTasks.value = { 'model-a': download({ status: 'error', errorMessage: 'Last error' }) }
  details.showExecutionLogs('download-a')
  details.downloadTasks.value = {}
  details.showExecutionLogs('download-a')
  assert.equal(details.executionLogError.value, 'Last error')
  assert.equal(details.executionLogDownload.value.status, 'error')

  details.showExecutionLogs('missing-task')
  assert.equal(details.executionDownloadSnapshot.value, null)
  assert.equal(details.executionLogError.value, '')
})

test('healthy diagnostics start collapsed and newly reported warnings and errors expand them', async t => {
  const details = environment(t)
  assert.equal(details.diagnosticDetailsOpen.value, false)
  details.diagnostics.value[0].state = 'warn'
  await nextTick()
  assert.equal(details.diagnosticDetailsOpen.value, true)

  details.diagnosticDetailsOpen.value = false
  details.diagnostics.value[0].state = 'error'
  await nextTick()
  assert.equal(details.diagnosticDetailsOpen.value, true)
  details.diagnostics.value[0].state = 'ok'
  await nextTick()
  assert.equal(details.diagnosticDetailsOpen.value, true, 'a refresh should preserve the user-opened details')
})

test('locked-page diagnostic transitions cannot leave healthy details expanded after enabling', async t => {
  const details = environment(t, { enabled: false, state: 'error' })
  assert.equal(details.diagnosticDetailsOpen.value, false)
  details.diagnostics.value[0].state = 'warn'
  await nextTick()
  assert.equal(details.diagnosticDetailsOpen.value, false)
  details.diagnostics.value[0].state = 'ok'
  await nextTick()
  details.developerMode.value = true
  await nextTick()
  assert.equal(details.diagnosticDetailsOpen.value, false)

  details.diagnostics.value[0].state = 'error'
  await nextTick()
  assert.equal(details.diagnosticDetailsOpen.value, true)
  details.developerMode.value = false
  await nextTick()
  assert.equal(details.diagnosticDetailsOpen.value, false)
  details.diagnostics.value[0].state = 'ok'
  await nextTick()
  details.developerMode.value = true
  await nextTick()
  assert.equal(details.diagnosticDetailsOpen.value, false)
})

for (const state of ['warn', 'error']) {
  test(`enabling diagnostics with an existing ${state} expands the details`, async t => {
    const details = environment(t, { enabled: false, state })
    details.developerMode.value = true
    await nextTick()
    assert.equal(details.diagnosticDetailsOpen.value, true)
    details.diagnosticDetailsOpen.value = false
    details.developerMode.value = false
    await nextTick()
    details.developerMode.value = true
    await nextTick()
    assert.equal(details.diagnosticDetailsOpen.value, true)
  })
}
