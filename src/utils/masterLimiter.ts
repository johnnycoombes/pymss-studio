import type { EditorMasterLimiter, EditorSession } from '../types/editor'

export const DEFAULT_MASTER_LIMITER: Readonly<EditorMasterLimiter> = Object.freeze({
  enabled: false, gainDb: 0, ceilingDb: 0, attackMs: 3, releaseMs: 250,
})

export function normalizeMasterLimiter(value?: Partial<EditorMasterLimiter> | null): EditorMasterLimiter {
  const number = (key: Exclude<keyof EditorMasterLimiter, 'enabled'>, min: number, max: number) => {
    const raw = value?.[key]
    return typeof raw === 'number' && Number.isFinite(raw)
      ? Math.max(min, Math.min(max, raw)) : DEFAULT_MASTER_LIMITER[key]
  }
  return {
    enabled: value?.enabled === true,
    gainDb: number('gainDb', 0, 30), ceilingDb: number('ceilingDb', -30, 0),
    attackMs: number('attackMs', 0.1, 100), releaseMs: number('releaseMs', 5, 2000),
  }
}

/** Render at the encoding rate so resampling cannot introduce new peaks after limiting. */
export function masterLimiterRenderRate(project: EditorSession, requested?: number | 'auto') {
  if (typeof requested === 'number') return requested
  const solo = project.tracks.some(track => track.solo)
  const ids = new Set(project.tracks.filter(track => !track.muted && (!solo || track.solo)).flatMap(track =>
    track.clips?.length ? track.clips.filter(clip => !clip.muted && clip.duration > 0).map(clip => clip.assetId) : [track.sourceId]))
  const rates = project.sources.filter(source => ids.has(source.id) && !source.missing)
    .map(source => source.sampleRate).filter(rate => Number.isFinite(rate) && rate >= 8000)
  return Math.min(48000, rates.length ? Math.max(...rates) : 48000)
}
