import assert from 'node:assert/strict'
import test from 'node:test'
import { formatLogRecord, logClock, logContextPreview, logMessagePreview, normalizeLogEntries, normalizeLogRecord, workerLogRecord } from '../src/utils/logRecord.ts'
import { appendWorkflowNodeLogs } from '../src/workflows/nodeLogs.ts'

const sample = {
  schemaVersion: 1, id: 'pymss-1', timestamp: '2026-10-19T12:00:00.123Z',
  source: 'pymss', level: 'info', logger: 'pymss.separator', event: 'logging.record',
  message: 'No error detected; previous allocation failed safely',
  taskId: 'task-a', nodeId: 'step1', context: { model: 'model.ckpt' },
  location: { file: 'separator.py', line: 42 },
}

test('structured severity and provenance survive JSONL without message keyword inference', () => {
  const record = normalizeLogRecord(JSON.stringify(sample))
  assert.equal(record.id, sample.id)
  assert.equal(record.level, 'info')
  assert.equal(record.source, 'pymss')
  assert.equal(record.message, sample.message)
  assert.equal(record.location?.line, 42)
  const rendered = formatLogRecord(record)
  assert.equal(rendered.match(/INFO/g)?.length, 1)
  assert.equal(rendered.match(/\[pymss\]/g)?.length, 1)
  assert.ok(!rendered.includes('schemaVersion'))
})

test('copied records retain complete context, location, scope and multiline details', () => {
  const record = normalizeLogRecord({ ...sample,
    jobId: 'job-a', requestId: 'request-a', detail: 'Traceback:\n  separator.forward()\nRuntimeError: allocation failed',
    location: { file: 'D:/runtime/pymss/separator.py', line: 42, function: 'separate' },
    context: {
      command: 'infer_workflow', model: 'model.ckpt', python: 'D:/runtime/python.exe',
      worker: 'D:/app/python/worker.py', payloadFile: 'D:/Temp/payload.json',
      configuration: { device: 'cuda', overlap: 0.25, enabled: false, workflow: { nodes: [{ id: 'step1' }] } },
      parameters: { outputFormat: 'wav' }, taskIds: ['task-a', 'task-b'], operationId: 'operation-a',
      processId: 123, thread: 'MainThread', _summary: { truncated: false },
    },
  })
  const original = structuredClone(record)
  const preview = logMessagePreview(record)
  const copied = formatLogRecord(record)
  const clock = logClock(record.timestamp)
  assert.equal(copied.split(clock).length - 1, 1)
  assert.equal(copied.match(/INFO/g)?.length, 1)
  assert.equal(copied.match(/\[pymss\]/g)?.length, 1)
  assert.ok(copied.includes('(task=task-a node=step1 job=job-a request=request-a)'))
  assert.ok(copied.includes(`\n${record.detail}\n`))
  const metadata = JSON.parse(copied.slice(copied.indexOf('\n{') + 1))
  assert.deepEqual(metadata, { location: original.location, context: original.context })
  assert.deepEqual(record, original)
  assert.equal(logMessagePreview(record), preview)
  assert.equal(preview, `${sample.message} · command=infer_workflow · model=model.ckpt · python=D:/runtime/python.exe · worker=D:/app/python/worker.py · payloadFile=D:/Temp/payload.json · device=cuda`)
})

test('message previews expose command and result fields without changing the original message', () => {
  const record = normalizeLogRecord({ ...sample, event: 'worker.exit', message: 'worker.exit', context: {
    command: 'runtime_info', durationMs: 2733, status: 'exit code: 0', processId: 1234,
  } })
  assert.equal(logMessagePreview(record), 'worker.exit · command=runtime_info · durationMs=2733 · status=exit code: 0')
  assert.equal(record.message, 'worker.exit')
  assert.equal(record.context.processId, 1234)
})

test('message previews stay on one line while expanded messages retain the complete text', () => {
  const message = 'Decoder failed\n  Unsupported audio stream'
  const record = normalizeLogRecord({ ...sample, message, context: { command: 'infer_workflow', code: 0 } })
  assert.equal(logMessagePreview(record), 'Decoder failed Unsupported audio stream · command=infer_workflow · code=0')
  assert.equal(record.message, message)
})

test('context previews remain separate from the message and retain diagnostic values', () => {
  const record = normalizeLogRecord({ ...sample, message: 'Worker started', context: {
    command: 'env_info', code: 0, hasPayload: false, taskId: 'task-a', processId: 1234,
    empty: '', absent: null, raw: 'original payload',
  } })
  assert.equal(logContextPreview(record), 'command=env_info · code=0 · hasPayload=false')
  assert.equal(record.message, 'Worker started')
  assert.equal(record.context.raw, 'original payload')
  assert.equal(logContextPreview(normalizeLogRecord({ ...sample, context: {} })), '')
})

test('unknown stderr remains unclassified and a canonical exception stays one record', () => {
  const raw = normalizeLogRecord({ ...sample, source: 'third_party', level: null, message: 'Decoder initialized' })
  assert.equal(raw.level, null)
  const detail = 'Traceback (most recent call last):\n  File "model.py", line 2\nRuntimeError: out of memory'
  const entries = normalizeLogEntries([{ ...normalizeLogRecord(sample), level: 'error', detail }])
  assert.equal(entries.length, 1)
  assert.equal(entries[0].detail, detail)
})

test('operation previews keep runtime snapshots in details while exposing the selected model and counts', () => {
  const record = normalizeLogRecord({ ...sample, message: 'Operation requested', context: {
    _summary: { truncated: false }, command: 'infer_workflow', operationId: 'internal-id',
    configuration: { model: 'model.ckpt', device: 'cuda', workflow: { nodes: [{ id: 'node-1' }] } },
    tasks: { total: 80, items: [{ taskId: 'task-a' }] },
  } })
  assert.equal(logContextPreview(record), 'command=infer_workflow · tasksCount=80 · model=model.ckpt · device=cuda')
  assert.equal(record.context.operationId, 'internal-id')
  assert.deepEqual(record.context.configuration, { model: 'model.ckpt', device: 'cuda', workflow: { nodes: [{ id: 'node-1' }] } })
})

test('the collector record is shared between event and node log and duplicate delivery does not append twice', () => {
  const record = workerLogRecord({ type: 'task_log', taskId: 'task-a', payload: { level: 'warning', message: 'outer', logRecord: sample } })
  assert.equal(record.message, sample.message)
  assert.equal(record.level, 'info')
  const target = { nodeLogs: {} }
  appendWorkflowNodeLogs(target, 'step1', record)
  appendWorkflowNodeLogs(target, 'step1', record)
  assert.equal(target.nodeLogs.step1.length, 1)
  assert.equal(target.nodeLogs.step1[0].id, sample.id)
})

test('historical nested pymss lines adapt once while ordinary paths do not decide the source', () => {
  const old = '1792407070.344Z WARN worker.stderr taskId=task-a message="20:56:41 | ERR | pymss/separator.py:42 | Allocation failed"'
  const record = normalizeLogRecord(old)
  assert.equal(record.level, 'error')
  assert.equal(record.source, 'pymss')
  assert.equal(record.message, 'Allocation failed')
  assert.equal(record.taskId, 'task-a')
  const studio = normalizeLogRecord('1792407070.344Z INFO app.paths path="D:/pymss/logs"')
  assert.equal(studio.source, 'studio')
  assert.equal(studio.level, 'info')
})

test('legacy traceback continuation remains readable without changing a later structured record', () => {
  const records = normalizeLogEntries(['Traceback (most recent call last):', '  File "model.py", line 1', 'RuntimeError: failed', normalizeLogRecord(sample)])
  assert.deepEqual(records.map(record => record.level), ['error', 'error', 'error', 'info'])
})
