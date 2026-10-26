import assert from 'node:assert/strict'
import test from 'node:test'

import { calculateEditorPanelCapacity, resolveEditorPanelLayout } from '../src/utils/editorLayout.ts'

const baseMetrics = {
  shellWidth: 980,
  minCenterWidth: 520,
  assetRailWidth: 34,
  inspectorRailWidth: 34,
  resizerWidth: 10,
  assetPanelWidth: 218,
  inspectorPanelWidth: 268,
  assetPanelVisible: true,
  inspectorRailVisible: true,
  inspectorPanelVisible: false,
}

test('a collapsed inspector does not reserve its full panel width', () => {
  const capacity = calculateEditorPanelCapacity(baseMetrics)
  assert.equal(capacity.centerWidth, 684)
  assert.equal(capacity.availableAssetWidth, 382)
})

test('inspector capacity accounts for the visible asset panel', () => {
  const capacity = calculateEditorPanelCapacity(baseMetrics)
  assert.equal(capacity.availableInspectorWidth, 154)
})

test('collapsing the asset panel makes room for the inspector without shrinking the timeline', () => {
  const capacity = calculateEditorPanelCapacity({
    ...baseMetrics,
    assetPanelVisible: false,
    inspectorRailVisible: false,
    inspectorPanelVisible: true,
  })
  assert.equal(capacity.centerWidth, 668)
  assert.equal(capacity.availableInspectorWidth, 416)
})

const responsiveMetrics = {
  ...baseMetrics, minAssetWidth: 180, maxAssetWidth: 320, minInspectorWidth: 240, maxInspectorWidth: 340,
  assetRequested: true, inspectorRequested: true, priority: 'inspector' as const,
}

test('responsive panels preserve the timeline and stay within bounds across desktop widths', () => {
  for (const shellWidth of [980, 994, 1024, 1280, 1366, 1920, 2560, 3840]) {
    const layout = resolveEditorPanelLayout({ ...responsiveMetrics, shellWidth, assetPanelWidth: 320, inspectorPanelWidth: 340 })
    const capacity = calculateEditorPanelCapacity({ ...baseMetrics, ...layout, shellWidth,
      inspectorRailVisible: layout.inspectorVisible && !layout.inspectorPanelVisible })
    assert.ok(capacity.centerWidth >= 520, `${shellWidth}: ${capacity.centerWidth}`)
    assert.ok(layout.inspectorPanelWidth >= 240 && layout.inspectorPanelWidth <= 340)
    assert.ok(layout.assetPanelWidth >= 180 && layout.assetPanelWidth <= 320)
  }
})

test('temporary narrowing fits both minimum panels before hiding the lower-priority panel', () => {
  const preferences = { ...responsiveMetrics, assetPanelWidth: 320, inspectorPanelWidth: 340 }
  const constrained = resolveEditorPanelLayout({ ...preferences, shellWidth: 1024 })
  assert.equal(constrained.assetPanelVisible, true)
  assert.equal(constrained.assetPanelWidth, 180)
  assert.equal(constrained.inspectorPanelWidth, 270)
  const narrow = resolveEditorPanelLayout({ ...preferences, shellWidth: 980 })
  assert.equal(narrow.assetPanelVisible, false)
  assert.equal(narrow.inspectorPanelVisible, true)
  const wide = resolveEditorPanelLayout({ ...preferences, shellWidth: 1920 })
  assert.equal(wide.assetPanelWidth, 320)
  assert.equal(wide.inspectorPanelWidth, 340)
  assert.equal(wide.assetPanelVisible, true)
  assert.equal(preferences.assetPanelWidth, 320)
})

test('the most recently opened panel wins only while there is insufficient space', () => {
  const layout = resolveEditorPanelLayout({ ...responsiveMetrics, shellWidth: 980, priority: 'assets' })
  assert.equal(layout.assetPanelVisible, true)
  assert.equal(layout.inspectorPanelVisible, false)
  const wide = resolveEditorPanelLayout({ ...responsiveMetrics, shellWidth: 1920, priority: 'assets' })
  assert.equal(wide.assetPanelVisible, true)
  assert.equal(wide.inspectorPanelVisible, true)
})

test('an explicit collapsed choice stays collapsed after space becomes available', () => {
  const layout = resolveEditorPanelLayout({ ...responsiveMetrics, shellWidth: 2560, assetRequested: false })
  assert.equal(layout.assetPanelVisible, false)
  assert.equal(layout.inspectorPanelVisible, true)
})

test('small fallback windows retain the inspector with a usable timeline', () => {
  const layout = resolveEditorPanelLayout({ ...responsiveMetrics, shellWidth: 800 })
  assert.equal(layout.assetPanelVisible, false)
  assert.equal(layout.inspectorPanelVisible, true)
  const capacity = calculateEditorPanelCapacity({ ...baseMetrics, ...layout, shellWidth: 800,
    inspectorRailVisible: false })
  assert.ok(capacity.centerWidth >= 360)
})
