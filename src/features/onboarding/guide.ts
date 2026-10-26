import { PARAMETER_GUIDES, type ParameterGuideId } from './parameters.ts'

export type BeginnerGuideStatus = 'unseen' | 'pending' | 'active' | 'skipped' | 'completed'
export type BeginnerGuideProgress = { status: BeginnerGuideStatus; step: number; stepId?: string; parameter?: ParameterGuideId }
export type BeginnerGuideChapter = 'models' | 'separation' | 'results' | 'workflows' | 'tools' | 'settings'
type BeginnerGuideStep = {
  id: string
  chapter: BeginnerGuideChapter
  route: string
  anchor: string | null
  title: string
  description: string
  section?: string
  tool?: string
  hiddenOnMac?: boolean
  parameters?: 'inference' | 'output'
  requiresModelMode?: boolean
  fallback?: string
}

export const BEGINNER_GUIDE_CHAPTERS = {
  models: 'beginnerGuide.chapters.models', separation: 'beginnerGuide.chapters.separation',
  results: 'beginnerGuide.chapters.results', workflows: 'beginnerGuide.chapters.workflows',
  tools: 'beginnerGuide.chapters.tools', settings: 'beginnerGuide.chapters.settings',
} as const

export const BEGINNER_GUIDE_STEPS: readonly BeginnerGuideStep[] = [
  { id: 'workspace', chapter: 'models', route: '/', anchor: 'workspace-navigation', title: 'beginnerGuide.workspaceTitle', description: 'beginnerGuide.workspaceDescription' },
  { id: 'modelSearch', chapter: 'models', route: '/models', anchor: 'model-filters', title: 'beginnerGuide.filtersTitle', description: 'beginnerGuide.filtersDescription' },
  { id: 'modelChoice', chapter: 'models', route: '/models', anchor: 'model-library', fallback: 'model-library-fallback', title: 'beginnerGuide.modelTitle', description: 'beginnerGuide.modelDescription' },
  { id: 'modelManage', chapter: 'models', route: '/models', anchor: 'model-management', title: 'beginnerGuide.managementTitle', description: 'beginnerGuide.managementDescription' },
  { id: 'input', chapter: 'separation', route: '/', anchor: 'input', requiresModelMode: true, title: 'beginnerGuide.inputTitle', description: 'beginnerGuide.inputDescription' },
  { id: 'modelSelection', chapter: 'separation', route: '/', anchor: 'separation-models', fallback: 'task-queue', requiresModelMode: true, title: 'beginnerGuide.selectionTitle', description: 'beginnerGuide.selectionDescription' },
  { id: 'output', chapter: 'separation', route: '/', anchor: 'output-location', requiresModelMode: true, title: 'beginnerGuide.outputTitle', description: 'beginnerGuide.outputDescription' },
  { id: 'format', chapter: 'separation', route: '/', anchor: 'output-format', requiresModelMode: true, title: 'beginnerGuide.formatTitle', description: 'beginnerGuide.formatDescription' },
  { id: 'stems', chapter: 'separation', route: '/', anchor: 'output-stems', requiresModelMode: true, title: 'beginnerGuide.stemsTitle', description: 'beginnerGuide.stemsDescription' },
  { id: 'naming', chapter: 'separation', route: '/', anchor: 'output-naming', requiresModelMode: true, title: 'beginnerGuide.namingTitle', description: 'beginnerGuide.namingDescription' },
  { id: 'inference', chapter: 'separation', route: '/', anchor: null, parameters: 'inference', requiresModelMode: true, title: 'beginnerGuide.inferenceTitle', description: 'beginnerGuide.inferenceDescription' },
  { id: 'encoding', chapter: 'separation', route: '/', anchor: null, parameters: 'output', requiresModelMode: true, title: 'beginnerGuide.encodingTitle', description: 'beginnerGuide.encodingDescription' },
  { id: 'run', chapter: 'separation', route: '/', anchor: 'start-separation', fallback: 'task-queue', requiresModelMode: true, title: 'beginnerGuide.runTitle', description: 'beginnerGuide.runDescription' },
  { id: 'queue', chapter: 'separation', route: '/', anchor: 'task-queue', title: 'beginnerGuide.queueTitle', description: 'beginnerGuide.queueDescription' },
  { id: 'results', chapter: 'results', route: '/results', anchor: 'result-item', fallback: 'results-empty', title: 'beginnerGuide.resultsTitle', description: 'beginnerGuide.resultsDescription' },
  { id: 'resultEditor', chapter: 'results', route: '/results', anchor: 'result-actions', fallback: 'results-empty', title: 'beginnerGuide.editorTitle', description: 'beginnerGuide.editorDescription' },
  { id: 'editorPlayback', chapter: 'results', route: '/results', anchor: 'demo-editor-transport', title: 'beginnerGuide.editorPlaybackTitle', description: 'beginnerGuide.editorPlaybackDescription' },
  { id: 'editorMix', chapter: 'results', route: '/results', anchor: 'demo-editor-mixer', title: 'beginnerGuide.editorMixTitle', description: 'beginnerGuide.editorMixDescription' },
  { id: 'editorClips', chapter: 'results', route: '/results', anchor: 'demo-editor-clip', title: 'beginnerGuide.editorClipsTitle', description: 'beginnerGuide.editorClipsDescription' },
  { id: 'editorExport', chapter: 'results', route: '/results', anchor: 'demo-editor-export', title: 'beginnerGuide.editorExportTitle', description: 'beginnerGuide.editorExportDescription' },
  { id: 'ensemble', chapter: 'workflows', route: '/', anchor: 'model-mode', fallback: 'separation-modes', title: 'beginnerGuide.ensembleTitle', description: 'beginnerGuide.ensembleDescription' },
  { id: 'workflowMode', chapter: 'workflows', route: '/', anchor: 'separation-modes', title: 'beginnerGuide.workflowModeTitle', description: 'beginnerGuide.workflowModeDescription' },
  { id: 'workflowCreate', chapter: 'workflows', route: '/workflows', anchor: 'workflow-actions', title: 'beginnerGuide.workflowTitle', description: 'beginnerGuide.workflowDescription' },
  { id: 'workflowOverview', chapter: 'workflows', route: '/workflows', anchor: 'workflow-overview', title: 'beginnerGuide.workflowRunTitle', description: 'beginnerGuide.workflowRunDescription' },
  { id: 'workflowEditor', chapter: 'workflows', route: '/workflows', anchor: 'demo-workflow-editor', title: 'beginnerGuide.workflowEditorTitle', description: 'beginnerGuide.workflowEditorDescription' },
  { id: 'workflowOutput', chapter: 'workflows', route: '/workflows', anchor: 'demo-workflow-output', title: 'beginnerGuide.workflowOutputTitle', description: 'beginnerGuide.workflowOutputDescription' },
  { id: 'toolsOverview', chapter: 'tools', route: '/tools', anchor: 'audio-tools', title: 'beginnerGuide.toolsOverviewTitle', description: 'beginnerGuide.toolsOverviewDescription' },
  { id: 'paths', chapter: 'settings', route: '/settings', section: 'paths', anchor: 'settings-paths', title: 'beginnerGuide.pathsTitle', description: 'beginnerGuide.pathsDescription' },
  { id: 'device', chapter: 'settings', route: '/settings', section: 'defaults', anchor: 'settings-device', title: 'beginnerGuide.deviceTitle', description: 'beginnerGuide.deviceDescription' },
  { id: 'downloads', chapter: 'settings', route: '/settings', section: 'defaults', anchor: 'settings-downloads', title: 'beginnerGuide.downloadsTitle', description: 'beginnerGuide.downloadsDescription' },
  { id: 'runtime', chapter: 'settings', route: '/settings', section: 'runtime', anchor: 'runtime-overview', title: 'beginnerGuide.runtimeTitle', description: 'beginnerGuide.runtimeDescription' },
  { id: 'help', chapter: 'settings', route: '/settings', section: 'about', anchor: 'guide-help', title: 'beginnerGuide.helpTitle', description: 'beginnerGuide.helpDescription' },
]

const LEGACY_STEP_IDS = ['modelChoice', 'input', 'output', 'inference', 'encoding', 'run', 'queue', 'results', 'modelSearch', 'modelManage', 'ensemble', 'workflowCreate', 'workflowOverview', 'toolsOverview', 'runtime', 'help']

export function normalizeBeginnerGuideProgress(value: unknown): BeginnerGuideProgress {
  const raw = value && typeof value === 'object' ? value as Record<string, unknown> : {}
  const status = ['unseen', 'pending', 'active', 'skipped', 'completed'].includes(String(raw.status))
    ? raw.status as BeginnerGuideStatus : 'unseen'
  const rawStep = typeof raw.step === 'number' && Number.isInteger(raw.step) ? raw.step : 0
  let savedId = typeof raw.stepId === 'string' ? raw.stepId : status === 'active' ? LEGACY_STEP_IDS[rawStep] : undefined
  if (['toolInspect', 'toolConvert', 'toolTempo', 'toolSdr', 'toolSlicer', 'toolMerge', 'toolLoudness', 'toolMidi', 'toolAsr'].includes(String(savedId))) savedId = 'toolsOverview'
  let step = BEGINNER_GUIDE_STEPS.findIndex(item => item.id === savedId)
  if (step < 0) step = Math.max(0, Math.min(BEGINNER_GUIDE_STEPS.length - 1, rawStep))
  let parameterGuide = PARAMETER_GUIDES.find(item => item.id === raw.parameter)
  if (status === 'active' && raw.stepId === undefined && raw.parameter === undefined && (rawStep === 3 || rawStep === 4)) {
    parameterGuide = PARAMETER_GUIDES.find(item => item.id === (rawStep === 3 ? 'chunk_size' : 'overlap_size'))
  }
  if (status === 'active' && BEGINNER_GUIDE_STEPS[step]?.parameters && parameterGuide) {
    step = BEGINNER_GUIDE_STEPS.findIndex(item => item.parameters === parameterGuide.group)
  }
  return { status, step, stepId: BEGINNER_GUIDE_STEPS[step]!.id, ...(parameterGuide ? { parameter: parameterGuide.id } : {}) }
}
