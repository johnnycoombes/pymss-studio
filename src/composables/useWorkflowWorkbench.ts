import { computed, onBeforeUnmount, onMounted, ref, watch, type ComputedRef } from 'vue'
import { getCurrentWebview } from '@tauri-apps/api/webview'
import { invoke } from '@tauri-apps/api/core'
import { useI18n } from 'vue-i18n'
import { useMessage } from 'naive-ui'
import { useWorkbenchStore } from '@/stores/workbench'
import { useTaskStore } from '@/stores/task'
import { useSettingsStore } from '@/stores/settings'
import type { WorkflowEntry } from '@/stores/workflow'
import { isMainTaskOwner } from '@/utils/taskOwner'
import { detectWorkflowFormat } from '@/workflows/formats'
import { initializeWorkbenchBridge, requestWorkbenchAction } from '@/utils/workbenchBridge'
import { cloneWorkflow, prepareWorkbenchRun, visibleWorkflowNodeStates, workbenchOutputDirectory, workflowBindingsEqual, workflowExecutionSignature, workflowInputNodes, workflowOutputNodes } from '@/workflows/workbench'
import { visibleWorkflowNodeLogs } from '@/workflows/nodeLogs'
import { activeWorkflowTask, selectedWorkflowJob, workflowBatchExecution } from '@/workflows/batchExecution'

export function useWorkflowWorkbench(options: {
  workflowKey: ComputedRef<string>
  definition: () => Record<string, unknown>
  save: () => Promise<WorkflowEntry | null>
}) {
  const store = useWorkbenchStore()
  const tasks = useTaskStore()
  const settings = useSettingsStore()
  const { t } = useI18n()
  const message = useMessage()
  const submitting = ref(false)
  const session = computed(() => store.session(options.workflowKey.value))
  const viewMode = computed({ get: () => session.value.viewMode, set: value => { session.value.viewMode = value } })
  const selectedJobId = computed({ get: () => session.value.selectedJobId, set: value => { session.value.selectedJobId = value } })
  const owner = isMainTaskOwner()
  const jobs = computed(() => owner ? tasks.allJobs.filter(job => job.primary.runConfig?.workbench && job.primary.runConfig.workflowId === options.workflowKey.value) : store.remoteJobs[options.workflowKey.value] || [])
  const focusedJob = computed(() => selectedWorkflowJob(jobs.value, selectedJobId.value))
  const focusedTask = computed(() => activeWorkflowTask(focusedJob.value?.tasks || []) || focusedJob.value?.tasks.at(-1) || null)
  const batchExecution = computed(() => workflowBatchExecution(focusedJob.value?.tasks || []))
  const executionDefinition = computed(() => focusedTask.value?.runConfig?.workbench?.sourceDefinition || null)
  const executionMatchesDraft = computed(() => Boolean(executionDefinition.value)
    && workflowExecutionSignature(executionDefinition.value!) === workflowExecutionSignature(options.definition())
    && (focusedTask.value!.runConfig!.workbench!.nativeGraph
      || workflowBindingsEqual(session.value.bindings, focusedTask.value!.runConfig!.workbench!.bindings)))
  const executionNodeSummaries = computed(() => {
    const target = focusedTask.value
    const snapshot = target?.runConfig?.workbench
    if (!target || !snapshot) return {}
    const summary: Record<string, string> = {}
    for (const node of workflowInputNodes(snapshot.sourceDefinition)) {
      const path = snapshot.inputs[`studio_input_${node.id}`] || target.input
      summary[node.id] = path.split(/[\\/]/).pop() || path
    }
    for (const node of workflowOutputNodes(snapshot.sourceDefinition)) summary[node.id] = snapshot.outputs[node.id] || ''
    summary.save = [...new Set(Object.values(snapshot.outputs))].join(' | ')
    return summary
  })
  const running = computed(() => Boolean(focusedJob.value?.tasks.some(task => !['done', 'failed', 'cancelled'].includes(task.status))))
  const nodeSummaries = computed(() => Object.fromEntries([
    ...workflowInputNodes(options.definition()).map(node => {
      const files = session.value.bindings.inputs[node.id]?.files || (node.embeddedPath ? [node.embeddedPath] : [])
      return [node.id, files.length ? `${files.length} · ${files[0].split(/[\\/]/).pop()}` : t('workbench.inputCount', { count: 0 })]
    }),
    ...workflowOutputNodes(options.definition()).map(node => [node.id, workbenchOutputDirectory(node, session.value.bindings.outputs[node.id], settings.outputDir)]),
  ]))
  const nodeStates = computed(() => running.value
    ? executionDefinition.value ? visibleWorkflowNodeStates(executionDefinition.value, focusedTask.value?.nodeStates) : {}
    : batchExecution.value.states)
  const liveNodeStates = computed(() => executionMatchesDraft.value ? nodeStates.value : {})
  const nodeLogs = computed(() => running.value
    ? executionDefinition.value ? visibleWorkflowNodeLogs(executionDefinition.value, focusedTask.value?.nodeLogs) : {}
    : batchExecution.value.logs)
  const liveNodeLogs = computed(() => executionMatchesDraft.value ? nodeLogs.value : {})
  watch(() => [options.workflowKey.value, workflowInputNodes(options.definition()).map(node => node.id).join('\n'),
    workflowOutputNodes(options.definition()).map(node => node.id).join('\n')], () => {
    const inputs = new Set(workflowInputNodes(options.definition()).map(node => node.id))
    const outputs = new Set(workflowOutputNodes(options.definition()).map(node => node.id))
    for (const key of Object.keys(session.value.bindings.inputs)) if (!inputs.has(key)) delete session.value.bindings.inputs[key]
    for (const key of Object.keys(session.value.bindings.outputs)) if (!outputs.has(key)) delete session.value.bindings.outputs[key]
  })
  let unlistenDrop: (() => void) | undefined
  const stopRemoteWatches: (() => void)[] = []
  let remoteReady = false
  let adoptedKey = ''
  let disposed = false
  onMounted(async () => {
    if (typeof window === 'undefined' || !('__TAURI_INTERNALS__' in window)) return
    if (!owner) {
      try {
        const connected = await initializeWorkbenchBridge()
        if (!connected || disposed) return
        stopRemoteWatches.push(watch(options.workflowKey, async (key, previous) => {
          remoteReady = false
          try {
            if (adoptedKey === key) {
              adoptedKey = ''
              await connected.remember(key, cloneWorkflow(store.session(key)))
            } else if (previous && store.sessions[previous]) {
              await connected.remember(previous, cloneWorkflow(store.session(previous)))
            }
            await connected.attach(key)
            if (!disposed && options.workflowKey.value === key) {
              remoteReady = true
              await connected.remember(key, cloneWorkflow(store.session(key)))
            }
          } catch (error) { if (!disposed) message.error(String(error)) }
        }, { immediate: true }))
        stopRemoteWatches.push(watch(() => session.value, value => {
          if (remoteReady && !disposed) void connected.remember(options.workflowKey.value, cloneWorkflow(value)).catch(error => message.error(String(error)))
        }, { deep: true }))
      } catch (error) { message.error(String(error)) }
    }
    if (detectWorkflowFormat(options.definition()) !== 'simple') return
    try {
      const remove = await getCurrentWebview().onDragDropEvent(async event => {
        if (event.payload.type !== 'drop') return
        const nodes = workflowInputNodes(options.definition())
        const position = event.payload.position
        const target = document.elementFromPoint(position.x / window.devicePixelRatio, position.y / window.devicePixelRatio)?.closest<HTMLElement>('[data-workbench-input]')
        const id = target?.dataset.workbenchInput || session.value.bindings.primaryInput || (nodes.length === 1 ? nodes[0].id : '')
        if (!id || !nodes.some(node => node.id === id)) { message.warning(t('workbench.missingInput')); return }
        try {
          const scanned = await invoke<{ files: string[] }>('scan_media_paths', { paths: event.payload.paths })
          const previous = session.value.bindings.inputs[id]?.files || []
          session.value.bindings.inputs[id] = { files: [...new Set([...previous, ...scanned.files])] }
          session.value.bindings.primaryInput ||= id
        } catch (error) { message.error(String(error)) }
      })
      if (disposed) remove()
      else unlistenDrop = remove
    } catch { /* Browser previews and older webviews use the explicit file controls. */ }
  })
  onBeforeUnmount(() => { disposed = true; unlistenDrop?.(); stopRemoteWatches.splice(0).forEach(stop => stop()) })
  function adoptWorkflowKey(key: string) {
    if (key === options.workflowKey.value) return
    store.adopt(options.workflowKey.value, key)
    adoptedKey = key
  }
  async function run() {
    if (submitting.value) return
    submitting.value = true
    try {
      const originalKey = options.workflowKey.value
      const bindings = cloneWorkflow(session.value.bindings)
      prepareWorkbenchRun(options.definition(), bindings, settings.outputDir)
      const entry = await options.save()
      if (!entry) return
      store.adopt(originalKey, entry.id)
      let result: { jobId?: string; total?: number }
      if (owner) result = await tasks.startWorkbenchInference(entry, bindings)
      else {
        const connected = await initializeWorkbenchBridge()
        if (!connected) throw new Error('workbench.hostUnavailable')
        await connected.remember(entry.id, cloneWorkflow(store.session(entry.id)))
        await connected.attach(entry.id)
        result = await requestWorkbenchAction({ action: 'run', workflowKey: entry.id, entry: cloneWorkflow(entry), bindings })
      }
      const target = store.session(entry.id)
      target.selectedJobId = result.jobId || ''
      target.selectedTaskId = ''
      target.viewMode = 'edit'
      message.success(t('workbench.submitted', { count: result.total }))
    } catch (error) {
      const text = error instanceof Error ? error.message : String(error)
      message.error(/^(workbench|workflows)\./.test(text) ? t(text) : text)
    } finally { submitting.value = false }
  }
  async function cancel() {
    if (!focusedJob.value) return
    try {
      if (owner) await tasks.cancelWorkbenchJob(focusedJob.value.id)
      else await requestWorkbenchAction({ action: 'cancel', workflowKey: options.workflowKey.value, jobId: focusedJob.value.id })
    } catch (error) { message.error(String(error)) }
  }
  return { session, viewMode, selectedJobId, focusedJob, focusedTask, executionDefinition, executionNodeSummaries, nodeStates, liveNodeStates, nodeLogs, liveNodeLogs, executionMatchesDraft, nodeSummaries, running, submitting, run, cancel, adoptWorkflowKey }
}
