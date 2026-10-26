import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { parse } from 'vue/compiler-sfc'

const descriptor = parse(readFileSync(new URL('../src/features/plugin-marketplace/PluginViewHost.vue', import.meta.url), 'utf8')).descriptor
const source = ts.createSourceFile('host.ts', descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true)
const names = new Set(['allowed', 'handleCall', 'postToFrame', 'onWindowMessage'])
const snippets = source.statements.flatMap(node => {
  if (ts.isFunctionDeclaration(node) && names.has(node.name?.text)) return [node.getText(source)]
  if (ts.isExpressionStatement(node) && ts.isCallExpression(node.expression)) {
    const call = node.expression
    if (['onMounted', 'onBeforeUnmount'].includes(call.expression.getText(source))) {
      return [`const ${call.expression.getText(source)}Callback = ${call.arguments[0].getText(source)}`]
    }
  }
  return []
})
const code = ts.transpileModule(snippets.join('\n'), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText

function host(overrides = {}) {
  const messages = []
  const state = {
    disposed: false, runRequests: new Map(), unlistenWorker: undefined, themeObserver: undefined,
    props: { pluginName: 'gain', pluginsRoot: '/plugins', view: { id: 'editor', entry: 'ui/index.html' } },
    permissions: () => ['operations'],
    frame: { value: { contentWindow: { postMessage: value => messages.push(value) } } },
    src: { value: '' }, status: { value: 'loading' }, errorText: { value: '' },
    window: { addEventListener() {}, removeEventListener() {} }, document: { documentElement: {} },
    MutationObserver: class { observe() {} disconnect() {} },
    onWindowMessage() {}, pushTheme() {},
    pluginViewUrl: () => 'http://pymss-plugin.localhost/view/gain/editor/index.html', convertFileSrc() {},
    listen: async () => () => {}, invoke: async () => {}, ...overrides,
  }
  const controller = vm.runInNewContext(`${code}\n({handleCall, postToFrame, onWindowMessage, mount: onMountedCallback, unmount: onBeforeUnmountCallback})`, state)
  return { state, controller, messages }
}

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

test('native operation start failure removes its pending SDK request', async () => {
  const failure = new Error('worker unavailable')
  const { state, controller } = host({ invoke: async () => { throw failure } })
  await assert.rejects(controller.handleCall(1, 'operations.start', {}), error => error === failure)
  assert.equal(state.runRequests.size, 0)
})

test('unmount releases a listener that resolves after the view is closed', async () => {
  const listener = deferred()
  const started = deferred()
  let unlistened = 0
  const { state, controller } = host({ listen: () => { started.resolve(); return listener.promise } })
  const mounted = controller.mount()
  await started.promise
  assert.equal(state.src.value, '')
  controller.unmount()
  listener.resolve(() => { unlistened++ })
  await mounted
  assert.equal(unlistened, 1)
  assert.equal(state.src.value, '')
})

test('listener setup failure becomes a view error', async () => {
  const { state, controller } = host({ listen: async () => { throw new Error('listen unavailable') } })
  await controller.mount()
  assert.equal(state.status.value, 'error')
  assert.match(state.errorText.value, /listen unavailable/)
  assert.equal(state.src.value, '')
})

test('the frame waits for the native plugin root and event listener', async () => {
  const root = deferred()
  const commands = []
  const { state, controller } = host({ invoke: (command, args) => {
    commands.push([command, args.root])
    return root.promise
  } })
  const mounted = controller.mount()
  assert.equal(state.src.value, '')
  root.resolve()
  await mounted
  assert.deepEqual(commands, [['set_plugin_view_root', '/plugins']])
  assert.match(state.src.value, /^http:/)
})

test('unmount rejects an operation while native startup is pending', async () => {
  const startup = deferred()
  const { state, controller, messages } = host({ invoke: () => startup.promise })
  const running = controller.handleCall(1, 'operations.start', {})
  assert.equal(state.runRequests.size, 1)
  controller.unmount()
  assert.equal(state.runRequests.size, 0)
  controller.postToFrame({ result: 'late' })
  assert.deepEqual(messages, [])
  startup.resolve()
  await assert.rejects(running, /PLUGIN_VIEW_CLOSED/)
})

test('the actual SDK correlates concurrent progress and waits for terminal responses', async () => {
  let workerListener
  const starts = []
  const { state, controller } = host({
    listen: async (_event, callback) => { workerListener = callback; return () => {} },
    invoke: async (command, args) => { if (command === 'start_operation_task') starts.push(args.payload) },
  })
  await controller.mount()
  const sdkWindow = { parent: { postMessage: message => {
    queueMicrotask(() => controller.onWindowMessage({ source: state.frame.value.contentWindow, data: message }))
  } } }
  let receive
  sdkWindow.addEventListener = (name, callback) => { if (name === 'message') receive = callback }
  state.frame.value.contentWindow.postMessage = message => queueMicrotask(() => receive({ data: message }))
  vm.runInNewContext(readFileSync(new URL('../src-tauri/resources/plugin-sdk/pymss-sdk.js', import.meta.url), 'utf8'), { window: sdkWindow })
  await sdkWindow.pymss.ready()
  const progress = [[], []]
  let finished = false
  const first = sdkWindow.pymss.startOperation('test.first', {}, {}, value => { progress[0].push(value.fraction); throw new Error('plugin callback') })
    .then(value => { finished = true; return value })
  const second = sdkWindow.pymss.startOperation('test.second', {}, {}, value => progress[1].push(value.fraction))
  await new Promise(setImmediate)
  assert.equal(starts.length, 2)
  const emit = (index, type, payload) => workerListener({ payload: { type, requestId: starts[index].requestId, payload } })
  emit(1, 'operation_progress', { fraction: 0.75 })
  emit(0, 'operation_progress', { fraction: 0.25 })
  await new Promise(setImmediate)
  assert.deepEqual(progress, [[0.25], [0.75]])
  assert.equal(finished, false)
  emit(0, 'operation_result', { outputs: { value: 1 } })
  emit(1, 'operation_result', { outputs: { value: 2 } })
  assert.equal((await first).outputs.value, 1)
  assert.equal((await second).outputs.value, 2)
  emit(0, 'operation_progress', { fraction: 1 })
  await new Promise(setImmediate)
  assert.deepEqual(progress, [[0.25], [0.75]])
  assert.equal(state.runRequests.size, 0)
  controller.unmount()
})
