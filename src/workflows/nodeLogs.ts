import { visibleWorkflowNodeId } from './workbench.ts'
import type { LogEntry } from '../utils/logRecord.ts'

export type WorkflowNodeLogs = Record<string, LogEntry[]>

export function appendWorkflowNodeLogs(target: { nodeLogs?: WorkflowNodeLogs }, nodeId: string | undefined, lines: LogEntry | LogEntry[]) {
  if (!nodeId) return
  const values = (Array.isArray(lines) ? lines : [lines]).flatMap<LogEntry>(line => typeof line === 'string' ? line.split(/\r?\n/).filter(Boolean) : [line])
  if (!values.length) return
  const previous = target.nodeLogs || {}
  const existing = Object.hasOwn(previous, nodeId) ? previous[nodeId] : []
  const unique = values.filter(value => typeof value === 'string' || value.legacy || !existing.some(entry => typeof entry !== 'string' && entry.id === value.id))
  const logs = { ...previous, [nodeId]: [...existing, ...unique].slice(-120) }
  let total = Object.values(logs).reduce((sum, items) => sum + items.length, 0)
  for (const key of Object.keys(logs)) {
    if (total <= 600) break
    if (key === nodeId) continue
    const remove = Math.min(total - 600, logs[key].length)
    logs[key] = logs[key].slice(remove)
    total -= remove
    if (!logs[key].length) delete logs[key]
  }
  target.nodeLogs = logs
}

export function visibleWorkflowNodeLogs(definition: Record<string, unknown>, logs: WorkflowNodeLogs = {}): WorkflowNodeLogs {
  const result: WorkflowNodeLogs = {}
  for (const [id, lines] of Object.entries(logs)) {
    const visible = visibleWorkflowNodeId(id, definition)
    if (!visible) continue
    Object.defineProperty(result, visible, { value: [...(Object.hasOwn(result, visible) ? result[visible] : []), ...lines].slice(-120), enumerable: true, configurable: true, writable: true })
  }
  return result
}
