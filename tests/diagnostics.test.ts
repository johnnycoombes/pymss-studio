import assert from 'node:assert/strict'
import test from 'node:test'
import { buildDiagnosticChecks, isDiagnosticIssueEvent, summarizeDiagnosticChecks } from '../src/utils/diagnostics.ts'

const ready = {
  env: { pythonVersion: '3.12.10', pymssAvailable: true, torchAvailable: true, torchBackend: 'cpu', cudaAvailable: false, avAvailable: true, librosaAvailable: true, customModelsSupported: true },
  runtime: { ready: true, torchBackend: 'cpu', pymssGraphAvailable: true, packages: { audioread: true } },
  connection: 'connected' as const,
  isMac: false,
  optionalPackage: { package: 'funasr', requirement: 'funasr==1.3.26', installed: false, version: null, source: null, runtimeIdentity: 'cpu-py312' } as const,
}

test('an unprobed environment remains unknown instead of reporting missing dependencies', () => {
  const checks = buildDiagnosticChecks({ env: null, runtime: null, connection: 'idle', isMac: false })
  assert.ok(checks.every(check => check.state === 'unknown'))
  assert.equal(summarizeDiagnosticChecks(checks).state, 'unknown')
})

test('CPU and an uninstalled optional speech package do not make a healthy runtime fail', () => {
  const checks = buildDiagnosticChecks(ready)
  assert.equal(checks.find(check => check.key === 'accelerator')?.state, 'ok')
  assert.equal(checks.find(check => check.key === 'funasr')?.state, 'optional')
  assert.equal(summarizeDiagnosticChecks(checks).state, 'ok')
})

test('failed probes override previously healthy data and preserve the failure reason', () => {
  const checks = buildDiagnosticChecks({ ...ready, runtimeError: 'Python failed to start' })
  assert.equal(checks.find(check => check.key === 'runtime')?.state, 'error')
  assert.equal(checks.find(check => check.key === 'runtime')?.detail, 'Python failed to start')
  assert.equal(summarizeDiagnosticChecks(checks).state, 'error')
})

test('graph support and decoder dependencies are checked individually', () => {
  const checks = buildDiagnosticChecks({ ...ready, runtime: { ...ready.runtime, pymssGraphAvailable: false, packages: { audioread: false } } })
  assert.equal(checks.find(check => check.key === 'graph')?.state, 'warn')
  assert.equal(checks.find(check => check.key === 'audioread')?.state, 'warn')
  assert.equal(summarizeDiagnosticChecks(checks).state, 'warn')
})

test('device errors, incompatible optional packages and disconnected event listeners stay visible', () => {
  const checks = buildDiagnosticChecks({ ...ready, env: { ...ready.env, cudaDeviceNamesError: 'Device enumeration failed' }, optionalPackage: { ...ready.optionalPackage, present: true, issue: 'version_mismatch' }, connection: 'error', connectionError: 'Listener failed' })
  assert.equal(checks.find(check => check.key === 'accelerator')?.detail, 'Device enumeration failed')
  assert.equal(checks.find(check => check.key === 'funasr')?.state, 'warn')
  assert.equal(checks.find(check => check.key === 'connection')?.detail, 'Listener failed')
})

test('macOS hides unsupported speech dependencies and missing optional probe data is unknown', () => {
  assert.ok(!buildDiagnosticChecks({ ...ready, isMac: true }).some(check => check.key === 'funasr'))
  const checks = buildDiagnosticChecks({ ...ready, optionalPackage: null, optionalError: 'Probe failed' })
  assert.equal(checks.find(check => check.key === 'funasr')?.state, 'unknown')
  assert.equal(summarizeDiagnosticChecks(checks).state, 'unknown')
})

test('the detailed device probe takes priority over cached runtime acceleration flags', () => {
  const checks = buildDiagnosticChecks({ ...ready, env: { ...ready.env, torchBackend: 'cuda' }, runtime: { ...ready.runtime, torchBackend: 'cuda', acceleratorAvailable: true } })
  assert.equal(checks.find(check => check.key === 'accelerator')?.state, 'warn')
  assert.equal(checks.find(check => check.key === 'accelerator')?.value, 'CPU')
})

test('issue filtering recognizes workflow failures while cancellation remains informational', () => {
  for (const event of ['node_failed', 'execution_failed']) {
    assert.equal(isDiagnosticIssueEvent({ type: 'workflow_execution', payload: { version: 1, event } }), true, event)
  }
  for (const event of ['node_cancelled', 'execution_cancelled']) {
    assert.equal(isDiagnosticIssueEvent({ type: 'workflow_execution', payload: { version: 1, event } }), false)
  }
})

test('issue filtering recognizes Python logging severity even when the task succeeds', () => {
  for (const level of ['warning', 'warn', 'error', 'critical', 'fatal', 'ERROR']) {
    assert.equal(isDiagnosticIssueEvent({ type: 'task_log', payload: { level, message: 'Decoder fallback' } }), true, level)
  }
})

test('download warnings remain visible after a successful fallback', () => {
  const events = [
    { type: 'download_log', payload: { level: 'warning', message: 'aria2c failed; retrying with urllib' } },
    { type: 'download_done', payload: { model: 'voice.ckpt', downloaded: ['D:/models/voice.ckpt'] } },
  ]
  assert.equal(isDiagnosticIssueEvent(events[0]), true)
  assert.equal(events.some(isDiagnosticIssueEvent), true)
})

test('download log severity excludes normal messages and retains errors', () => {
  assert.equal(isDiagnosticIssueEvent({ type: 'download_log', payload: { level: 'error', message: 'Download failed' } }), true)
  assert.equal(isDiagnosticIssueEvent({ type: 'download_log', payload: { level: 'info', message: 'No error detected' } }), false)
  assert.equal(isDiagnosticIssueEvent({ type: 'download_log', payload: { level: 'debug', message: 'Retry state' } }), false)
})

test('issue filtering preserves terminal error events and excludes normal progress', () => {
  assert.equal(isDiagnosticIssueEvent({ type: 'error', payload: { code: 'INVALID_INPUT' } }), true)
  assert.equal(isDiagnosticIssueEvent({ type: 'task_cancelled' }), false)
  assert.equal(isDiagnosticIssueEvent({ type: 'task_log', payload: { level: 'info', message: 'No error detected' } }), false)
  assert.equal(isDiagnosticIssueEvent({ type: 'workflow_execution', payload: { event: 'node_completed' } }), false)
  assert.equal(isDiagnosticIssueEvent({ type: 'workflow_execution', payload: { event: 'node_skipped' } }), false)
  assert.equal(isDiagnosticIssueEvent({ type: 'task_done' }), false)
  assert.equal(isDiagnosticIssueEvent({ type: 'task_log', payload: null }), false)
})
