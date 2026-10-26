import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import { Window } from 'happy-dom'
import { logRecordRenderKeys, normalizeLogEntries, normalizeLogRecord } from '../src/utils/logRecord.ts'

const browser = new Window()
for (const key of ['window', 'document', 'Node', 'Element', 'HTMLElement', 'SVGElement']) {
  Object.defineProperty(globalThis, key, { configurable: true, value: key === 'window' ? browser : browser[key] })
}
const { createApp, computed, h, nextTick, ref } = await import('vue')
after(() => browser.happyDOM.abort())

function record(id) {
  return normalizeLogRecord({ schemaVersion: 1, id, message: id, context: {} })
}

function mountLogRows(t, initial) {
  const entries = ref(initial)
  const warnings = []
  const container = document.createElement('div')
  document.body.append(container)
  const app = createApp({
    setup() {
      const records = computed(() => normalizeLogEntries(entries.value))
      const keys = computed(() => logRecordRenderKeys(records.value))
      return () => h('div', records.value.map((item, index) => h('div', {
        class: 'log-line', key: keys.value[index],
      }, item.message)))
    },
  })
  app.config.warnHandler = warning => warnings.push(warning)
  app.mount(container)
  t.after(() => { app.unmount(); container.remove() })
  return { entries, container, warnings }
}

test('shared console keys preserve duplicate records when a rolling log replaces both ends', async t => {
  const a = record('a')
  const b = record('b')
  const { entries, container, warnings } = mountLogRows(t, [a, b, a])
  const messages = () => [...container.querySelectorAll('.log-line')].map(row => row.textContent)
  assert.deepEqual(messages(), ['a', 'b', 'a'])
  entries.value = [b, a, b]
  await nextTick()
  assert.deepEqual(messages(), ['b', 'a', 'b'])
  assert.equal(container.querySelectorAll('.log-line').length, 3)
  assert.equal(warnings.length, 0)
  entries.value = [a, a, b, a]
  await nextTick()
  assert.deepEqual(messages(), ['a', 'a', 'b', 'a'])
  assert.equal(warnings.length, 0)
})

test('shared console keys keep repeated historical messages visible alongside shared batch records', async t => {
  const shared = record('shared-batch')
  const { entries, container, warnings } = mountLogRows(t, ['Loading model', shared, 'Loading model', shared])
  assert.equal(container.querySelectorAll('.log-line').length, 4)
  entries.value = [shared, 'Loading model', shared, 'Loading model']
  await nextTick()
  assert.deepEqual([...container.querySelectorAll('.log-line')].map(row => row.textContent), [
    'shared-batch', 'Loading model', 'shared-batch', 'Loading model',
  ])
  assert.equal(warnings.length, 0)
})
