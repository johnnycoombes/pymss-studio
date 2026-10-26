import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { computed, effectScope, nextTick, ref, watch } from 'vue'
import { parse } from 'vue/compiler-sfc'

const { descriptor } = parse(readFileSync(new URL('../src/views/SeparateView.vue', import.meta.url), 'utf8'))
const source = ts.createSourceFile('SeparateView.ts', descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true)
const names = new Set(['taskClockNow', 'taskClockTimer', 'currentTaskDuration', 'stopTaskClock', 'syncTaskClock', 'taskDuration'])
const statements = source.statements.filter(node => {
  if (ts.isFunctionDeclaration(node)) return names.has(node.name?.text)
  if (ts.isVariableStatement(node)) return node.declarationList.declarations.some(item => names.has(item.name.getText(source)))
  if (!ts.isExpressionStatement(node) || !ts.isCallExpression(node.expression)) return false
  return node.expression.expression.getText(source) === 'onBeforeUnmount'
    || (node.expression.expression.getText(source) === 'watch' && node.expression.arguments[1]?.getText(source) === 'syncTaskClock')
})
assert.equal(statements.length, names.size + 2)
const code = ts.transpileModule(statements.map(node => node.getText(source)).join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText

function task(overrides = {}) {
  return { id: 'task-1', status: 'separating', createdAt: 40000, startedAt: 100000, updatedAt: 100000, ...overrides }
}

function setup(t, item) {
  let now = 100000
  let timerId = 0
  const timers = new Map()
  const intervals = []
  const cleanup = []
  const currentTask = ref(item)
  const scope = effectScope()
  const state = scope.run(() => vm.runInNewContext(`${code}\n({ currentTaskDuration, taskClockNow })`, {
    ref, computed, watch, currentTask,
    Date: { now: () => now },
    setInterval: (callback, interval) => {
      intervals.push(interval)
      const id = timerId++
      timers.set(id, callback)
      return id
    },
    clearInterval: id => timers.delete(id),
    onBeforeUnmount: callback => cleanup.push(callback),
    unlistenDragDrop: null,
    finishInputPointerDrag: () => {},
    finishStemPointerDrag: () => {},
    stopAllPreviewAudio: () => {},
  }))
  const close = () => { cleanup.forEach(callback => callback()); scope.stop() }
  t.after(close)
  return {
    ...state, currentTask, timers, intervals, close,
    tick(milliseconds) {
      now += milliseconds
      for (const callback of timers.values()) callback()
    },
  }
}

test('elapsed time advances each second without receiving worker progress or log events', t => {
  const clock = setup(t, task())
  assert.equal(clock.currentTaskDuration.value, '0s')
  assert.deepEqual(clock.intervals, [1000])
  for (let seconds = 1; seconds <= 4; seconds += 1) {
    clock.tick(1000)
    assert.equal(clock.currentTaskDuration.value, `${seconds}s`)
    assert.equal(clock.currentTask.value.updatedAt, 100000)
  }
  clock.currentTask.value.updatedAt = 999000
  assert.equal(clock.currentTaskDuration.value, '4s')
})

test('queue time is excluded and stage transitions reuse one clock', async t => {
  const clock = setup(t, task({ status: 'queued', startedAt: undefined }))
  assert.equal(clock.timers.size, 0)
  clock.tick(30000)
  assert.equal(clock.currentTaskDuration.value, '0s')
  clock.currentTask.value.startedAt = 130000
  clock.currentTask.value.status = 'preparing'
  await nextTick()
  assert.equal(clock.currentTaskDuration.value, '0s')
  clock.tick(1000)
  assert.equal(clock.currentTaskDuration.value, '1s')
  for (const status of ['validating_input', 'loading_model', 'separating', 'writing_output']) {
    clock.currentTask.value.status = status
    await nextTick()
    assert.equal(clock.timers.size, 1)
  }
  assert.deepEqual(clock.intervals, [1000])
  clock.tick(59000)
  assert.equal(clock.currentTaskDuration.value, '1m 0s')
})

for (const status of ['done', 'failed', 'cancelled']) {
  test(`${status} freezes recorded execution time and stops the clock`, async t => {
    const clock = setup(t, task())
    clock.tick(37000)
    Object.assign(clock.currentTask.value, { status, durationMs: 37000, finishedAt: 137000, updatedAt: 200000 })
    await nextTick()
    assert.equal(clock.timers.size, 0)
    assert.equal(clock.currentTaskDuration.value, '37s')
    clock.tick(60000)
    clock.currentTask.value.updatedAt += 60000
    clock.taskClockNow.value += 60000
    assert.equal(clock.currentTaskDuration.value, '37s')
  })
}

test('terminal tasks without durationMs use finish time instead of subsequent updates', t => {
  const clock = setup(t, task({ status: 'done', startedAt: 50000, finishedAt: 87000, updatedAt: 200000 }))
  assert.equal(clock.currentTaskDuration.value, '37s')
  assert.equal(clock.timers.size, 0)
})

test('older terminal records remain readable without lifecycle timestamps', t => {
  const clock = setup(t, task({ status: 'done', createdAt: 10000, startedAt: undefined, updatedAt: 47000 }))
  assert.equal(clock.currentTaskDuration.value, '37s')
})

test('opening an existing running task shows current elapsed time immediately', t => {
  const clock = setup(t, task({ startedAt: 63000 }))
  assert.equal(clock.currentTaskDuration.value, '37s')
  clock.tick(1000)
  assert.equal(clock.currentTaskDuration.value, '38s')
})

test('switching running tasks keeps a single clock and uses the new start time', async t => {
  const clock = setup(t, task({ startedAt: 63000 }))
  clock.currentTask.value = task({ id: 'task-2' })
  await nextTick()
  assert.equal(clock.currentTaskDuration.value, '0s')
  assert.deepEqual(clock.intervals, [1000])
  clock.tick(1000)
  assert.equal(clock.currentTaskDuration.value, '1s')
})

test('leaving the running task or unmounting clears the interval', async t => {
  const clock = setup(t, null)
  assert.equal(clock.timers.size, 0)
  clock.currentTask.value = task()
  await nextTick()
  assert.equal(clock.timers.size, 1)
  clock.currentTask.value = null
  await nextTick()
  assert.equal(clock.timers.size, 0)
  assert.equal(clock.currentTaskDuration.value, '')
  clock.currentTask.value = task()
  await nextTick()
  clock.close()
  assert.equal(clock.timers.size, 0)
})
