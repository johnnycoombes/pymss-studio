import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { computed, effectScope, nextTick, reactive, ref, watch } from 'vue'
import { compileTemplate, parse } from 'vue/compiler-sfc'

const component = readFileSync(new URL('../src/components/editor/EditorPluginParameters.vue', import.meta.url), 'utf8')
const script = component.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
const source = ts.createSourceFile('parameters.ts', script, ts.ScriptTarget.Latest, true)
const code = ts.transpileModule(source.statements.filter(item => !ts.isImportDeclaration(item)).map(item => item.getText(source)).join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None },
}).outputText
const flush = async () => { await nextTick(); await new Promise(resolve => setImmediate(resolve)) }
const parameter = (overrides = {}) => ({ id: 'mix', name: 'Mix', min: 0, max: 1, default: 0, unit: 'None', taper: 'Linear', choices: [], ...overrides })
class PluginHostDialogPendingError extends Error {}
async function environment(t, response) {
  const props = reactive({ session: { id: 'project' }, slotId: 'effect', disabled: false })
  const calls = [], disposals = [], pending = []
  let reply = response || { params: [parameter()], values: { mix: 0.019999999552965164 } }
  let readError = null, observer = null
  const context = vm.createContext({
    Error,
    PluginHostDialogPendingError,
    observePluginSession(callback) { observer = callback; return () => { observer = null } },
    computed, ref, watch, defineProps: () => props, useI18n: () => ({ t: key => key }),
    onScopeDispose: callback => disposals.push(callback),
    async getPluginParameters(project, slotId) {
      calls.push({ op: 'read', project, slotId })
      const response = reply
      if (pending.length) await pending.shift()
      if (readError) throw readError
      return response
    },
    async setPluginParameter(project, slotId, id, value) { calls.push({ op: 'set', project, slotId, id, value }); return { value } },
  })
  const scope = effectScope()
  scope.run(() => vm.runInContext(code, context))
  t.after(() => { disposals.forEach(callback => callback()); scope.stop() })
  await flush()
  return {
    props, calls, pending, setResponse(value) { reply = value }, setReadError(error) { readError = error }, observe(host) { observer?.(host) },
    read: name => vm.runInContext(`${name}.value`, context),
    call(name, ...args) { context.__args = args; return vm.runInContext(`${name}(...__args)`, context) },
  }
}

test('display formatting removes binary float tails without changing plugin values or units', async t => {
  const env = await environment(t)
  for (const [raw, expected] of [[0.019999999552965164, '0.02'], [0.699999988079071, '0.7'], [1.2000000476837158, '1.2'], [48000.00001, '48000'], [-0, '0'], [0.00001234567, '0.0000123457']]) {
    assert.equal(env.call('formatValue', raw), expected)
  }
  assert.equal(env.call('formatValue', null), '')
  assert.equal(env.call('formatValue', NaN), '')
  assert.equal(env.read('values').mix, 0.019999999552965164)
  assert.equal(env.call('unitLabel', parameter({ unit: 'Hz' })), 'Hz')
  assert.equal(env.call('unitLabel', parameter({ unit: 'Ms' })), 'ms')
  assert.equal(env.calls.filter(call => call.op === 'set').length, 0)
})

test('parsing the unchanged displayed value preserves full precision and makes blur a no-op', async t => {
  const env = await environment(t)
  const param = parameter()
  const raw = env.read('values').mix
  const parsed = env.call('parseValue', param, env.call('formatValue', raw))
  assert.equal(parsed, raw)
  await env.call('commit', param, parsed)
  assert.equal(env.calls.filter(call => call.op === 'set').length, 0)
  assert.equal(env.read('drafts').mix, raw)
  assert.equal(env.call('parseValue', param, ''), null)
  assert.equal(env.call('parseValue', param, 'not a value'), null)
})

test('an explicit edit keeps its entered precision and reported range through the commit', async t => {
  const env = await environment(t)
  const param = parameter()
  const entered = env.call('parseValue', param, '0.123456789')
  await env.call('commit', param, entered)
  assert.equal(env.calls.find(call => call.op === 'set').value, 0.123456789)
  assert.equal(env.read('values').mix, 0.123456789)
  assert.equal(env.call('formatValue', env.read('values').mix), '0.123457')
  await env.call('commit', param, 2)
  assert.equal(env.calls.filter(call => call.op === 'set').at(-1).value, 1)
})

test('closing or switching the parameter dock cannot apply a late parameter list to another slot', async t => {
  const env = await environment(t)
  let release
  env.pending.push(new Promise(resolve => { release = resolve }))
  env.setResponse({ params: [parameter({ id: 'old', name: 'Old parameter' })], values: { old: 0.5 } })
  const old = env.call('refresh')
  env.setResponse({ params: [parameter({ id: 'new', name: 'New parameter' })], values: { new: 0.7 } })
  env.props.slotId = 'second'
  await flush()
  release()
  await old
  assert.equal(env.read('params')[0].id, 'new')
  assert.equal(env.read('values').new, 0.7)
  assert.equal('old' in env.read('values'), false)
})

test('a dialog wait refreshes parameters on recovery without retrying unrelated errors', async t => {
  const env = await environment(t)
  env.setReadError(new PluginHostDialogPendingError('Waiting for dialog'))
  await env.call('refresh')
  assert.equal(env.read('error'), 'Waiting for dialog')
  const reads = env.calls.length
  env.observe({ project: env.props.session, interactionRequired: true, unavailable: false })
  env.observe({ project: {}, interactionRequired: false, unavailable: false })
  assert.equal(env.calls.length, reads)
  env.setReadError(null)
  env.observe({ project: env.props.session, interactionRequired: false, unavailable: false })
  await flush()
  assert.equal(env.calls.length, reads + 1)
  assert.equal(env.read('error'), '')
  assert.equal(env.read('params')[0].id, 'mix')
  env.setReadError(new Error('Read failed'))
  await env.call('refresh')
  const failedReads = env.calls.length
  env.observe({ project: env.props.session, interactionRequired: false, unavailable: false })
  await flush()
  assert.equal(env.calls.length, failedReads)
})

test('the bounded parameter list and display formatter compile as an actual Vue template', () => {
  const parsed = parse(component, { filename: 'EditorPluginParameters.vue' })
  assert.deepEqual(parsed.errors, [])
  const result = compileTemplate({ filename: 'EditorPluginParameters.vue', id: 'parameter-dock', source: parsed.descriptor.template.content, compilerOptions: { expressionPlugins: ['typescript'] } })
  assert.deepEqual(result.errors, [])
})
