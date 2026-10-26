import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import test, { after } from 'node:test'
import { createPinia, setActivePinia } from 'pinia'
import { createServer } from 'vite'

const sourceRoot = fileURLToPath(new URL('../src', import.meta.url))
const rustCommandSource = fileURLToPath(new URL('../src-tauri/src/commands/app_cmd.rs', import.meta.url))

Object.defineProperty(globalThis, 'document', {
  configurable: true,
  value: { documentElement: { lang: 'en' } },
})

let invokeHandler = async () => null
Object.defineProperty(globalThis, 'window', {
  configurable: true,
  value: {
    addEventListener() {},
    matchMedia() {
      return { matches: false, addEventListener() {}, removeEventListener() {} }
    },
    __TAURI_INTERNALS__: {
      invoke(command, args) {
        return invokeHandler(command, args)
      },
    },
  },
})

const vite = await createServer({
  configFile: false,
  server: { watch: null, middlewareMode: true, hmr: false, preTransformRequests: false },
  appType: 'custom',
  optimizeDeps: { noDiscovery: true },
  resolve: { alias: { '@': sourceRoot } },
})
after(() => vite.close())

const { useEditorStore } = await vite.ssrLoadModule('/src/stores/editor.ts')
const { setLocale } = await vite.ssrLoadModule('/src/i18n/index.ts')

const source = (id, role, stemKey, name) => ({
  id,
  role,
  stemKey,
  path: `C:/audio/${name}.wav`,
  name: `${name}.wav`,
  duration: 10,
  sampleRate: 44_100,
  channels: 2,
  peaks: [0.1],
  channelPeaks: [[0.1], [0.1]],
  originKind: role === 'stem' ? 'task-result' : 'external',
})

const track = (id, sourceId, role, name) => ({
  id,
  sourceId,
  role,
  name,
  volume: 1,
  pan: 0,
  muted: false,
  solo: false,
  fadeIn: 0,
  fadeOut: 0,
})

test('editor localizes legacy default labels while preserving custom track names', async () => {
  const project = {
    version: 3,
    id: 'legacy-project',
    name: 'Song',
    masterVolume: 1,
    masterPan: 0,
    sources: [
      source('vocals-source', 'stem', 'vocals', 'vocals'),
      source('custom-source', 'stem', 'accompaniment', 'instrumental'),
      source('back-vocal-source', 'stem', 'back-vocal', 'back-vocal'),
      source('recording-source', 'recording', null, 'recording'),
    ],
    tracks: [
      track('vocals-track', 'vocals-source', 'stem', '人声'),
      track('custom-track', 'custom-source', 'stem', 'Lead Harmony'),
      { ...track('back-vocal-track', 'back-vocal-source', 'stem', 'back-vocal'), autoName: true },
      track('recording-track', 'recording-source', 'recording', '录音 2'),
    ],
    createdAt: 1,
    updatedAt: 1,
  }

  invokeHandler = async (command, args) => {
    if (command === 'load_editor_project') return structuredClone(project)
    if (command === 'get_audio_metadata') {
      return { path: args.payload.path, name: args.payload.path.split('/').at(-1), duration: 10, sampleRate: 44_100, channels: 2 }
    }
    if (command === 'save_editor_project') return args.project
    throw new Error(`Unexpected command: ${command}`)
  }

  setLocale('en')
  setActivePinia(createPinia())
  const editor = useEditorStore()
  await editor.loadProject(project.id)

  assert.deepEqual(editor.session.tracks.map(item => item.name), ['Vocals', 'Lead Harmony', 'back-vocal', 'Recording 2'])
  assert.deepEqual(editor.assetTree.map(item => item.name), ['Recordings', 'Separation Results'])

  setLocale('zh-CN')
  editor.localizeDefaultTrackNames()
  assert.deepEqual(editor.session.tracks.map(item => item.name), ['人声', 'Lead Harmony', 'back-vocal', '录音 2'])
  assert.deepEqual(editor.assetTree.map(item => item.name), ['分离结果', '录音'])

  editor.renameTrack('vocals-track', 'Custom Vocal')
  setLocale('en')
  editor.localizeDefaultTrackNames()
  assert.equal(editor.session.tracks[0].name, 'Custom Vocal')
  assert.equal(editor.session.tracks[0].autoName, false)
  await editor.flushSave()
})

test('new task projects store locale-neutral stem names', () => {
  const sourceCode = readFileSync(rustCommandSource, 'utf8')
  assert.match(sourceCode, /"name": stem,\s*"autoName": true,/)
  assert.doesNotMatch(sourceCode, /fn display_stem_name/)
})
