import assert from 'node:assert/strict'
import test from 'node:test'
import { formatLogRecord, normalizeLogEntries, normalizeLogRecord, type LogRecord } from '../src/utils/logRecord.ts'
import { groupLogTracebacks } from '../src/utils/logTracebacks.ts'

const header = 'Traceback (most recent call last):'
const frame = '  File "worker.py", line 10, in run'
const code = '    model.forward()'
const caret = '    ^^^^^^^^^^^^^^^'
const summary = 'RuntimeError: invalid shape'
const traceback = [header, frame, code, caret, summary]
const context = { processId: 120, thread: 'MainThread', operationId: 'operation-a' }
let sequence = 0

function record(message: string, patch: Partial<LogRecord> = {}): LogRecord {
  return normalizeLogRecord({
    schemaVersion: 1, id: `trace-line-${sequence++}`, timestamp: '2026-10-20T12:00:00.123Z',
    source: 'third_party', level: 'error', logger: 'stderr', event: 'process.stderr', message,
    taskId: 'task-a', nodeId: 'node-a', jobId: 'job-a', requestId: 'request-a',
    context: { ...context }, ...patch,
  })
}

function checkGroups(records: readonly LogRecord[], ranges: readonly (readonly [number, number])[]) {
  const groups = groupLogTracebacks(records)
  assert.deepEqual(groups.map(group => [group.firstIndex, group.lastIndex]), ranges)
  assert.equal(groups.flatMap(group => group.records).length, records.length)
  for (const group of groups) {
    assert.strictEqual(group.record, records[group.firstIndex])
    assert.equal(group.records.length, group.lastIndex - group.firstIndex + 1)
    group.records.forEach((item, index) => assert.strictEqual(item, records[group.firstIndex + index]))
    assert.equal(group.message, records.slice(group.firstIndex, group.lastIndex + 1).map(item => item.message).join('\n'))
  }
  return groups
}

test('legacy traceback lines form one paragraph with their original indentation', () => {
  const records = normalizeLogEntries(traceback)
  assert.equal(records[1].message, frame)
  assert.equal(records[2].message, code)
  assert.equal(records[3].message, caret)
  assert.equal(checkGroups(records, [[0, 4]])[0].message, traceback.join('\n'))
})

test('per-line ERROR and WARN metadata leave frame, source and caret indentation intact', () => {
  for (const prefix of ['[ERROR]', '[WARN]', '[warning]', 'error:', 'warning:']) {
    const records = normalizeLogEntries(traceback.map(line => `${prefix} ${line}`))
    assert.deepEqual(records.map(item => item.message), traceback, prefix)
    assert.equal(checkGroups(records, [[0, 4]])[0].message, traceback.join('\n'))
  }
})

test('legacy worker envelopes retain traceback indentation and contextual job scope', () => {
  const records = normalizeLogEntries(traceback.map((message, index) =>
    `1792407070.${String(index).padStart(3, '0')}Z WARN worker.stderr taskId=task-a nodeId=node-a jobId=job-a requestId=request-a message=${JSON.stringify(message)}`))
  assert.equal(records[1].message, frame)
  assert.equal(records[0].context.jobId, 'job-a')
  checkGroups(records, [[0, 4]])
})

test('both Python exception chaining separators and their blank lines remain in one paragraph', () => {
  const messages = [
    header, frame, 'ValueError: first failure', '',
    'During handling of the above exception, another exception occurred:', '',
    header, frame, 'RuntimeError: second failure', '',
    'The above exception was the direct cause of the following exception:', '',
    header, frame, 'ModelLoadFailure: checkpoint failed',
  ]
  checkGroups(normalizeLogEntries(messages), [[0, messages.length - 1]])
  checkGroups(messages.map(message => record(message)), [[0, messages.length - 1]])
})

test('exception types without messages remain attached to their traceback', () => {
  for (const exception of ['MemoryError', 'AssertionError', 'NotImplementedError', 'StopIteration', 'GeneratorExit', 'ModelLoadFailure', 'pymss.graph.errors.DAGError', 'E']) {
    const messages = [header, frame, code, exception]
    checkGroups(normalizeLogEntries(messages), [[0, 3]])
    checkGroups(messages.map(message => record(message)), [[0, 3]])
    for (const prefix of ['[ERROR]', '[WARN]']) {
      checkGroups(normalizeLogEntries(messages.map(message => `${prefix} ${message}`)), [[0, 3]])
    }
  }
})

test('message-free exceptions preserve both forms of chaining and their blank lines', () => {
  const messages = [
    header, frame, 'MemoryError', '',
    'During handling of the above exception, another exception occurred:', '',
    header, frame, 'AssertionError', '',
    'The above exception was the direct cause of the following exception:', '',
    header, frame, 'ModelLoadFailure',
  ]
  checkGroups(normalizeLogEntries(messages), [[0, messages.length - 1]])
  checkGroups(messages.map(message => record(message)), [[0, messages.length - 1]])
})

test('a message-free exception closes the traceback before unrelated output or another exception', () => {
  const messages = [header, frame, 'MemoryError', '  Recovery failed', 'Cleanup failed', header, frame, 'AssertionError']
  checkGroups(normalizeLogEntries(messages), [[0, 2], [3, 3], [4, 4], [5, 7]])
  checkGroups(messages.map(message => record(message)), [[0, 2], [3, 3], [4, 4], [5, 7]])
})

test('a multiline traceback ending in a bare exception type closes before subsequent output', () => {
  const messages = [[header, frame, code, 'MemoryError'].join('\n'), '  Recovery failed']
  checkGroups(normalizeLogEntries(messages), [[0, 0], [1, 1]])
  checkGroups(messages.map(message => record(message)), [[0, 0], [1, 1]])
})

test('the short traceback marker and immediately following Python header share one paragraph', () => {
  checkGroups(normalizeLogEntries(['traceback:', ...traceback]), [[0, 5]])
})

test('successive independent exceptions stay separate even with identical scope and reused IDs', () => {
  const first = traceback.map(message => record(message, { id: 'shared-record-id' }))
  const records = [...first, ...first]
  const groups = checkGroups(records, [[0, 4], [5, 9]])
  assert.strictEqual(groups[0].record, groups[1].record)
})

test('an explicit informational log closes an unfinished traceback and prevents resuming across it', () => {
  const records = [record(header), record(frame), record('  Recovering runtime', { level: 'info' }), record('  Runtime parameters', { level: 'info' })]
  checkGroups(records, [[0, 1], [2, 2], [3, 3]])
})

test('a warning after an exception keeps its own card and cannot be mistaken for a frame', () => {
  const records = normalizeLogEntries([...traceback, '[warning] File "optional-model.yaml" is missing', '[info] Cleanup completed'])
  const groups = checkGroups(records, [[0, 4], [5, 5], [6, 6]])
  assert.equal(groups[1].record.level, 'warn')
  assert.equal(groups[2].record.level, 'info')
})

test('changes in task, node, job or request scope break adjacent traceback fragments', () => {
  for (const field of ['taskId', 'nodeId', 'jobId', 'requestId'] as const) {
    const changed = { [field]: `${field}-other` }
    const records = [record(header), record(frame, changed), record(code, changed), record(summary, changed)]
    checkGroups(records, [[0, 0], [1, 3]])
  }
})

test('unknown task scope does not act as a wildcard for a following identified task', () => {
  checkGroups([record(header, { taskId: undefined }), record(frame), record(summary)], [[0, 0], [1, 2]])
})

const contextualScope = { taskId: 'task-a', nodeId: 'node-a', jobId: 'job-a', requestId: 'request-a' }
const noTopScope = { taskId: undefined, nodeId: undefined, jobId: undefined, requestId: undefined }

test('scope can fall back to context while matching equivalent top-level identifiers', () => {
  const records = [record(header), ...[frame, code, summary].map(message => record(message, {
    ...noTopScope, context: { ...context, ...contextualScope },
  }))]
  checkGroups(records, [[0, 3]])
})

test('each changed context scope fallback separates otherwise identical fragments', () => {
  for (const field of ['taskId', 'nodeId', 'jobId', 'requestId'] as const) {
    const original = { ...noTopScope, context: { ...context, ...contextualScope } }
    const changed = { ...noTopScope, context: { ...context, ...contextualScope, [field]: 'other' } }
    checkGroups([record(header, original), record(frame, changed), record(summary, changed)], [[0, 0], [1, 2]])
  }
})

test('interleaving another task interrupts adjacency rather than resuming a previous task by key', () => {
  const other = { taskId: 'task-b' }
  const records = [record(header), record(header, other), record(frame), record(summary)]
  checkGroups(records, [[0, 0], [1, 1], [2, 3]])
})

test('different worker processes, threads, operations and batch task sets cannot share a paragraph', () => {
  for (const changed of [
    { processId: 121 }, { thread: 'ThreadPool-2' }, { operationId: 'operation-b' },
    { taskIds: ['task-a', 'task-c'] }, { taskScope: 'operation' },
  ]) {
    const original = { ...noTopScope, context: { ...context, taskIds: ['task-a', 'task-b'] } }
    const next = { ...noTopScope, context: { ...original.context, ...changed } }
    checkGroups([record(header, original), record(frame, next), record(summary, next)], [[0, 0], [1, 2]])
  }
})

test('different logger or source boundaries retain independent records', () => {
  for (const changed of [{ logger: 'another-library' }, { source: 'worker' as const }]) {
    checkGroups([record(header), record(frame, changed), record(summary, changed)], [[0, 0], [1, 2]])
  }
})

test('unclassified structured stderr fragments form one traceback without rewriting their levels', () => {
  const records = traceback.map(message => record(message, { level: null }))
  checkGroups(records, [[0, 4]])
  assert.ok(records.every(item => item.level === null))
})

test('ProtocolLogHandler ERROR and WARN records group by traceback content rather than transport event', () => {
  for (const level of ['error', 'warn'] as const) {
    const producer = { source: 'pymss' as const, logger: 'pymss.separator', event: 'log', level }
    const records = [...traceback, 'Model retry failed'].map(message => record(message, producer))
    checkGroups(records, [[0, 4], [5, 5]])
    assert.ok(records.every(item => item.event === 'log' && item.level === level))
  }
})

test('ProtocolLogHandler exceptions with a canonical detail remain isolated from split logger records', () => {
  const producer = { source: 'pymss' as const, logger: 'pymss.separator', event: 'log' }
  const canonical = record('Cannot load model', { ...producer, detail: traceback.join('\n'), location: { file: 'separator.py', line: 50, function: 'load' } })
  const records = [record(header, producer), canonical, record(frame, producer), record(summary, producer)]
  checkGroups(records, [[0, 0], [1, 1], [2, 3]])
  assert.strictEqual(records[1], canonical)
  assert.equal(canonical.detail, traceback.join('\n'))
})

test('structured JSONL records group the same way as already decoded stderr fragments', () => {
  const original = traceback.map(message => record(message, { level: null }))
  const records = normalizeLogEntries(original.map(item => JSON.stringify(item)))
  assert.equal(records.length, original.length)
  checkGroups(records, [[0, 4]])
})

test('an intact canonical exception detail is never fused with neighboring fragments', () => {
  const canonical = record('Cannot load model', { detail: traceback.join('\n'), location: { file: 'model.py', line: 42, function: 'load' } })
  const records = [record(header), canonical, record(frame), record(summary)]
  checkGroups(records, [[0, 0], [1, 1], [2, 3]])
  assert.equal(canonical.detail, traceback.join('\n'))
})

test('a log limit that removed the traceback header still leaves one recognizable frame paragraph', () => {
  const records = normalizeLogEntries([frame, code, caret, 'KeyboardInterrupt'])
  checkGroups(records, [[0, 3]])
})

test('ordinary error messages remain separate even when their severity and scope match', () => {
  const records = [record('Model loading failed'), record('Download retry failed'), record('Configuration unavailable')]
  checkGroups(records, [[0, 0], [1, 1], [2, 2]])
})

test('streaming updates preserve original positions and stop the traceback before new normal output', () => {
  const records = [record('Starting inference', { level: 'info' }), record(header), record(frame)]
  checkGroups(records, [[0, 0], [1, 2]])
  records.push(record(code), record(summary))
  checkGroups(records, [[0, 0], [1, 4]])
  records.push(record('Cleanup completed', { level: 'info' }))
  checkGroups(records, [[0, 0], [1, 4], [5, 5]])
})

test('display grouping does not mutate records, collapse copy output or discard per-line context', () => {
  const records = traceback.map((message, index) => record(message, {
    id: `immutable-${index}`, context: { ...context, diagnosticLine: index, configuration: { device: 'cuda', overlap: 0.25 } },
    location: { file: 'stderr.py', line: index + 1 },
  }))
  const before = structuredClone(records)
  const copied = records.map(formatLogRecord).join('\n')
  for (const item of records) {
    Object.freeze(item.context)
    Object.freeze(item.location)
    Object.freeze(item)
  }
  Object.freeze(records)
  checkGroups(records, [[0, 4]])
  assert.deepEqual(records, before)
  assert.equal(records.map(formatLogRecord).join('\n'), copied)
  assert.ok(copied.includes('diagnosticLine'))
  assert.ok(copied.includes('configuration'))
  assert.ok(copied.includes('cuda'))
})

test('empty input produces no display groups', () => {
  assert.deepEqual(groupLogTracebacks([]), [])
})
