export type ParameterHelpTopic = 'chunk' | 'overlap' | 'vrWindow' | 'vrAggression'
  | 'tta' | 'vrTta' | 'standardize' | 'normalize' | 'batch' | 'bitDepth' | 'bitRate'

export const PARAMETER_HELP_COPY: Record<ParameterHelpTopic, { title: string; description: string; takeaway: string; detail: string }> = {
  chunk: { title: 'parameterHelp.chunkTitle', description: 'parameterHelp.chunkDescription', takeaway: 'parameterHelp.chunkTakeaway', detail: 'parameterHelp.chunkDetail' },
  overlap: { title: 'parameterHelp.overlapTitle', description: 'parameterHelp.overlapDescription', takeaway: 'parameterHelp.overlapTakeaway', detail: 'parameterHelp.overlapDetail' },
  vrWindow: { title: 'parameterHelp.vrWindowTitle', description: 'parameterHelp.vrWindowDescription', takeaway: 'parameterHelp.vrWindowTakeaway', detail: 'parameterHelp.vrWindowDetail' },
  vrAggression: { title: 'parameterHelp.vrAggressionTitle', description: 'parameterHelp.vrAggressionDescription', takeaway: 'parameterHelp.vrAggressionTakeaway', detail: 'parameterHelp.vrAggressionDetail' },
  tta: { title: 'parameterHelp.ttaTitle', description: 'parameterHelp.ttaDescription', takeaway: 'parameterHelp.ttaTakeaway', detail: 'parameterHelp.ttaDetail' },
  vrTta: { title: 'parameterHelp.vrTtaTitle', description: 'parameterHelp.vrTtaDescription', takeaway: 'parameterHelp.vrTtaTakeaway', detail: 'parameterHelp.vrTtaDetail' },
  standardize: { title: 'parameterHelp.standardizeTitle', description: 'parameterHelp.standardizeDescription', takeaway: 'parameterHelp.standardizeTakeaway', detail: 'parameterHelp.standardizeDetail' },
  normalize: { title: 'parameterHelp.normalizeTitle', description: 'parameterHelp.normalizeDescription', takeaway: 'parameterHelp.normalizeTakeaway', detail: 'parameterHelp.normalizeDetail' },
  batch: { title: 'parameterHelp.batchTitle', description: 'parameterHelp.batchDescription', takeaway: 'parameterHelp.batchTakeaway', detail: 'parameterHelp.batchDetail' },
  bitDepth: { title: 'parameterHelp.bitDepthTitle', description: 'parameterHelp.bitDepthDescription', takeaway: 'parameterHelp.bitDepthTakeaway', detail: 'parameterHelp.bitDepthDetail' },
  bitRate: { title: 'parameterHelp.bitRateTitle', description: 'parameterHelp.bitRateDescription', takeaway: 'parameterHelp.bitRateTakeaway', detail: 'parameterHelp.bitRateDetail' },
}

export function parameterHelpTopics(modelType?: string | null): ParameterHelpTopic[] {
  const type = String(modelType || '').trim().toLowerCase()
  if (type === 'vr') return ['vrWindow', 'vrAggression', 'vrTta', 'batch', 'normalize', 'bitDepth', 'bitRate']
  const common: ParameterHelpTopic[] = ['chunk', 'overlap', 'tta', 'batch', 'standardize', 'normalize', 'bitDepth', 'bitRate']
  return type ? common : [...common, 'vrWindow', 'vrAggression', 'vrTta']
}

export const PARAMETER_EXAMPLE = { chunkSize: 485100, sampleRate: 44100 } as const

export function msstOverlapSize(chunkSize: number, count: number): number | null {
  if (!Number.isSafeInteger(chunkSize) || !Number.isSafeInteger(count) || count < 1 || chunkSize < count) return null
  return chunkSize - Math.floor(chunkSize / count)
}
