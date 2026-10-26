import test from 'node:test'
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

const require = createRequire(import.meta.url)
const { parse, compileTemplate } = require('vue/compiler-sfc')
const ts = require('typescript')
const { createSSRApp, h } = require('vue')
const { renderToString } = require('vue/server-renderer')

const filename = new URL('../src/features/plugin-ops/PluginOpsTool.vue', import.meta.url)
const template = parse(readFileSync(filename, 'utf8'), { filename: filename.pathname }).descriptor.template?.content
assert.ok(template, 'PluginOpsTool must have a template')
const compiled = compileTemplate({ source: template, filename: filename.pathname, id: 'plugin-ops-ssr', ssr: true, ssrCssVars: [] })
assert.deepEqual(compiled.errors, [], `template compile errors: ${compiled.errors.join('\n')}`)
const cjs = ts.transpileModule(compiled.code, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const mod = { exports: {} }
vm.runInNewContext(cjs, { module: mod, exports: mod.exports, require, console })

// A slot-preserving Naive UI stand-in lets the compiled template execute as SSR
// while keeping this test about the real SFC render branches.
const Stub = (_props, { slots }) => h('div', slots.default?.())
const components = Object.fromEntries([
  'n-empty', 'n-spin', 'n-select', 'n-input', 'n-switch', 'n-input-number', 'n-button', 'n-alert',
].map(name => [name, Stub]))

test('operation results render structured values and preserve audio and scalars', async () => {
const outputs = [
  { kind: 'audio_file', path: '/out/vocals.wav' },
  { kind: 'scalar', value: 42 },
  { kind: 'object', value: { stems: [{ kind: 'audio_file', path: '/out/nested.wav' }], note: '<safe>' } },
  { kind: 'array', value: [{ name: 'score', value: 0.9 }] },
  { kind: 'unknown-kind', value: { diagnostic: 'visible fallback' } },
]
const ctx = {
  loading: false,
  state: { available: true, unavailableReason: '', specs: [{ id: 'nested', title: 'Nested', inputs: {}, params: { properties: {} } }] },
  selectedId: 'nested', selected: { id: 'nested', title: 'Nested', inputs: {}, params: { properties: {} } },
  running: false, runError: '', runResult: outputs, error: null,
  fileInputs: {}, paramValues: {}, t: key => key, reveal() {}, execute() {}, String, JSON,
}
const app = createSSRApp({ setup: () => ctx, ssrRender: mod.exports.ssrRender })
for (const [name, component] of Object.entries(components)) app.component(name, component)
const html = await renderToString(app)

assert.match(html, /\/out\/vocals\.wav/, 'audio_file branch must remain visible')
assert.match(html, />42</, 'scalar branch must remain visible')
assert.match(html, /<pre[^>]*>/, 'structured outputs require a readable fallback')
assert.match(html, /nested\.wav/, 'nested audio descriptor must be visible in fallback')
assert.match(html, /&lt;safe&gt;/, 'structured fallback must HTML-escape plugin data')
assert.match(html, /visible fallback/, 'unknown output kinds must remain visible')
})
