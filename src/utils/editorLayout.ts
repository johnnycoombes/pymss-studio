export type EditorLayoutMetrics = {
  shellWidth: number
  minCenterWidth: number
  assetRailWidth: number
  inspectorRailWidth: number
  resizerWidth: number
  assetPanelWidth: number
  inspectorPanelWidth: number
  assetPanelVisible: boolean
  inspectorRailVisible: boolean
  inspectorPanelVisible: boolean
}

export function calculateEditorPanelCapacity(metrics: EditorLayoutMetrics) {
  const inspectorRailWidth = metrics.inspectorRailVisible ? metrics.inspectorRailWidth : 0
  const assetPanelWidth = metrics.assetPanelVisible
    ? metrics.assetPanelWidth + metrics.resizerWidth
    : 0
  const inspectorPanelWidth = metrics.inspectorPanelVisible
    ? metrics.inspectorPanelWidth + metrics.resizerWidth
    : 0
  const fixedRailWidth = metrics.assetRailWidth + inspectorRailWidth

  return {
    centerWidth: Math.max(
      0,
      metrics.shellWidth - fixedRailWidth - assetPanelWidth - inspectorPanelWidth,
    ),
    availableAssetWidth: Math.max(
      0,
      metrics.shellWidth
        - metrics.minCenterWidth
        - fixedRailWidth
        - inspectorPanelWidth
        - metrics.resizerWidth,
    ),
    availableInspectorWidth: Math.max(
      0,
      metrics.shellWidth
        - metrics.minCenterWidth
        - fixedRailWidth
        - assetPanelWidth
        - metrics.resizerWidth,
    ),
  }
}

type ResponsivePanelMetrics = {
  shellWidth: number
  minCenterWidth: number
  assetRailWidth: number
  inspectorRailWidth: number
  resizerWidth: number
  assetPanelWidth: number
  inspectorPanelWidth: number
  minAssetWidth: number
  maxAssetWidth: number
  minInspectorWidth: number
  maxInspectorWidth: number
  assetRequested: boolean
  inspectorRequested: boolean
  priority: 'assets' | 'inspector'
}

/** Fit panels without rewriting the user's preferred widths or collapsed state. */
export function resolveEditorPanelLayout(metrics: ResponsivePanelMetrics) {
  const clamp = (value: number, min: number, max: number) => Math.min(Math.max(value, min), max)
  const width = Math.max(0, metrics.shellWidth)
  const inspectorVisible = width > 760
  const centerMinimum = Math.min(metrics.minCenterWidth, Math.max(360,
    width - metrics.assetRailWidth - metrics.resizerWidth - metrics.minInspectorWidth))
  let assetPanelVisible = metrics.assetRequested && width > 920
  let inspectorPanelVisible = metrics.inspectorRequested && inspectorVisible
  let assetPanelWidth = clamp(metrics.assetPanelWidth, metrics.minAssetWidth, metrics.maxAssetWidth)
  let inspectorPanelWidth = clamp(metrics.inspectorPanelWidth, metrics.minInspectorWidth, metrics.maxInspectorWidth)

  const bothMinimum = centerMinimum + metrics.assetRailWidth + metrics.resizerWidth * 2
    + metrics.minAssetWidth + metrics.minInspectorWidth
  if (assetPanelVisible && inspectorPanelVisible && width < bothMinimum) {
    if (metrics.priority === 'assets') inspectorPanelVisible = false
    else assetPanelVisible = false
  }
  if (assetPanelVisible && inspectorPanelVisible) {
    const budget = width - centerMinimum - metrics.assetRailWidth - metrics.resizerWidth * 2
    if (metrics.priority === 'assets') {
      assetPanelWidth = Math.min(assetPanelWidth, budget - metrics.minInspectorWidth)
      inspectorPanelWidth = Math.min(inspectorPanelWidth, budget - assetPanelWidth)
    } else {
      inspectorPanelWidth = Math.min(inspectorPanelWidth, budget - metrics.minAssetWidth)
      assetPanelWidth = Math.min(assetPanelWidth, budget - inspectorPanelWidth)
    }
  } else if (inspectorPanelVisible) {
    const budget = width - centerMinimum - metrics.assetRailWidth - metrics.resizerWidth
    inspectorPanelVisible = budget >= metrics.minInspectorWidth
    if (inspectorPanelVisible) inspectorPanelWidth = Math.min(inspectorPanelWidth, budget)
  }
  if (assetPanelVisible && !inspectorPanelVisible) {
    const budget = width - centerMinimum - metrics.assetRailWidth
      - (inspectorVisible ? metrics.inspectorRailWidth : 0) - metrics.resizerWidth
    assetPanelVisible = budget >= metrics.minAssetWidth
    if (assetPanelVisible) assetPanelWidth = Math.min(assetPanelWidth, budget)
  }
  return { inspectorVisible, assetPanelVisible, inspectorPanelVisible, assetPanelWidth, inspectorPanelWidth }
}
