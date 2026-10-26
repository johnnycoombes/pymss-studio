import { createApp, nextTick } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createWebHashHistory } from 'vue-router'
import { appRoutes } from '@/router/routes'
import i18n, { setLocale, type LocaleSetting } from '@/i18n'
import { initTheme } from '@/utils/theme'
import { installTutorialAdapter } from './demoContext'
import { createDemoRuntime, type DemoConfig } from './demoRuntime'
import { useSettingsStore } from '@/stores/settings'
import { useAppStore } from '@/stores/app'
import { useTaskStore } from '@/stores/task'
import { useModelStore } from '@/stores/model'
import { useWorkflowStore } from '@/stores/workflow'
import { useBeginnerGuideStore } from '@/stores/beginnerGuide'
import { connectWorkerEvents } from '@/utils/events'
import TutorialFrame from '@/components/onboarding/TutorialFrame.vue'

export async function bootstrapTutorial() {
  const parameters = new URLSearchParams(window.location.search)
  const config: DemoConfig = {
    locale: parameters.get('locale') || 'zh-CN', themeMode: parameters.get('theme') || 'dark',
    themeAccent: parameters.get('accent') || 'blue', platform: parameters.get('platform') || 'win32',
    progress: JSON.parse(parameters.get('progress') || '{}'),
  }
  const runtime = createDemoRuntime(config)
  installTutorialAdapter(runtime)
  ;(window as any).__tutorialRuntime = runtime
  ;(window as any).__pymssTutorialDispose = () => runtime.dispose()
  function privateStorage() {
    const values = new Map<string, string>()
    return { get length() { return values.size }, clear: () => values.clear(), key: (index: number) => [...values.keys()][index] || null,
      getItem: (key: string) => values.get(key) ?? null, removeItem: (key: string) => { values.delete(key) },
      setItem: (key: string, value: string) => { values.set(key, String(value)) } } satisfies Storage
  }
  Object.defineProperty(window, 'localStorage', { configurable: true, value: privateStorage() })
  Object.defineProperty(window, 'sessionStorage', { configurable: true, value: privateStorage() })
  if (!('__TAURI_INTERNALS__' in window)) Object.defineProperty(window, '__TAURI_INTERNALS__', { value: {
    metadata: { currentWindow: { label: 'main' }, currentWebview: { label: 'main' } }, plugins: {},
    transformCallback: runtime.callback, invoke: runtime.invoke, convertFileSrc: runtime.assetUrl,
    unregisterCallback() {}, runCallback() {},
  } })
  if (!('__TAURI_EVENT_PLUGIN_INTERNALS__' in window)) Object.defineProperty(window, '__TAURI_EVENT_PLUGIN_INTERNALS__', { value: { unregisterListener() {} } })
  window.open = () => null
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { enumerateDevices: async () => [], getUserMedia: async () => { throw new Error('Recording is unavailable in the tutorial') }, getSupportedConstraints: () => ({}), addEventListener() {}, removeEventListener() {} } })
  const originalFetch = window.fetch.bind(window)
  window.fetch = (input, options) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    if (!url.startsWith('blob:') && !url.startsWith('data:')) return Promise.reject(new Error('Only sample assets are available in this tutorial'))
    return originalFetch(input, options)
  }
  document.addEventListener('click', event => {
    const link = (event.target as Element)?.closest?.('a[href]')
    if (link) { event.preventDefault(); event.stopImmediatePropagation() }
  }, true)
  const routes = appRoutes.filter(route => !['editor', 'workflow-simple-editor', 'workflow-node-editor'].includes(String(route.name)))
  routes.push(
    { path: '/editor', name: 'editor', components: { default: () => import('@/views/ResultsView.vue'), popup: () => import('@/views/EditorView.vue') } },
    { path: '/workflow-simple-editor', name: 'workflow-simple-editor', components: { default: () => import('@/views/WorkflowsView.vue'), popup: () => import('@/views/WorkflowSimpleEditorView.vue') } },
    { path: '/workflow-node-editor', name: 'workflow-node-editor', components: { default: () => import('@/views/WorkflowsView.vue'), popup: () => import('@/views/WorkflowNodeEditorView.vue') } },
  )
  const router = createRouter({ history: createWebHashHistory(), routes })
  const pinia = createPinia()
  const application = createApp(TutorialFrame, { runtime })
  application.use(pinia).use(router).use(i18n)
  const settings = useSettingsStore(pinia)
  await settings.initialize()
  setLocale(config.locale as LocaleSetting)
  initTheme(settings.themeMode, settings.themeAccent)
  const app = useAppStore(pinia)
  app.runtimeInfo = runtime.runtimeInfo
  app.envInfo = runtime.envInfo
  app.setWorkerEventConnection('connected')
  const models = useModelStore(pinia)
  const task = useTaskStore(pinia)
  const workflow = useWorkflowStore(pinia)
  await Promise.all([models.initialize(), task.initialize(), workflow.initialize()])
  models.selectedModel = runtime.models[0]!.name
  await models.loadModels()
  await models.selectModel(runtime.models[0]!.name)
  await connectWorkerEvents(app)
  const guide = useBeginnerGuideStore(pinia)
  guide.setPlatform(config.platform === 'darwin')
  guide.start()
  await router.isReady()
  application.mount('#app')
  await nextTick()
  window.parent.postMessage({ type: 'pymss-tutorial-ready' }, '*')
  document.querySelectorAll('[data-tauri-drag-region]').forEach(element => element.removeAttribute('data-tauri-drag-region'))
  let disposed = false
  const dispose = () => { if (disposed) return; disposed = true; application.unmount(); runtime.dispose() }
  ;(window as any).__pymssTutorialDispose = dispose
  ;(window as any).__tutorialRuntime = runtime
  window.addEventListener('pagehide', dispose, { once: true })
}
