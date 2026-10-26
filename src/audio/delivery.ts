/** Shared output-format and 4-stem DJ rules for the Studio UI. */

export const OUTPUT_FORMAT_OPTIONS = [
  { label: 'WAV', value: 'wav' },
  { label: 'FLAC', value: 'flac' },
  { label: 'MP3', value: 'mp3' },
  { label: 'M4A', value: 'm4a' },
  { label: 'OGG', value: 'ogg' },
] as const

export const OGG_QUALITIES = [5, 6, 7, 8] as const
export const OGG_QUALITY_DEFAULT = 6
export const OGG_MIN_BITRATE_QUALITY = 8
export const DJ_STEMS = ['Vocals', 'Melody', 'Bass', 'Drums'] as const
export const DJ_NAMING_TEMPLATE = '%filename% - %stem%'
export const DJ_OGG_FILE_STEMS: Record<(typeof DJ_STEMS)[number], string> = {
  Vocals: 'vocals',
  Melody: 'other',
  Bass: 'bass',
  Drums: 'drums',
}

const XDJ_RESERVED_NAMES = new Set([
  'CON', 'PRN', 'AUX', 'NUL',
  ...Array.from({ length: 9 }, (_, index) => `COM${index + 1}`),
  ...Array.from({ length: 9 }, (_, index) => `LPT${index + 1}`),
])

export function xdjTrackFolderName(trackFilename: string): string {
  const base = String(trackFilename || '').replace(/\\/g, '/').split('/').pop() || ''
  const dot = base.lastIndexOf('.')
  // Match pathlib's stem: a trailing dot is part of the name, and a leading dot is not an extension.
  const text = dot > 0 && dot < base.length - 1 ? base.slice(0, dot) : base
  let folder = text.replace(/[<>:"/\\|?*\u0000-\u001f]+/g, '_')
  if (!folder || folder === '.' || folder === '..') folder = 'track'
  const device = folder.replace(/[ .]+$/g, '').split('.')[0].toUpperCase()
  if (folder.endsWith(' ') || folder.endsWith('.') || XDJ_RESERVED_NAMES.has(device)) folder = `${folder}_`
  return folder
}

const STOCK_NAMING_TEMPLATES = new Set([
  '',
  '%index%_%filename%_%stem%',
  '%filename%_%stem%',
  '%filename%_%stem%_%model%',
  '%stem%',
])

const VOCAL_ALIASES = new Set(['vocals', 'vocal', 'voice'])
const BASS_ALIASES = new Set(['bass'])
const DRUM_ALIASES = new Set(['drums', 'drum'])

export type DjRole = 'vocals' | 'bass' | 'drums' | 'melody'

export function clampOggQuality(value: unknown): number {
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return OGG_QUALITY_DEFAULT
  return Math.min(8, Math.max(5, Math.round(parsed)))
}

export function resolveOggQuality(quality: unknown, minBitrate: boolean): number {
  if (minBitrate) return OGG_MIN_BITRATE_QUALITY
  return clampOggQuality(quality)
}

export function djStemRole(name: unknown): DjRole | null {
  const key = String(name || '').trim().toLowerCase()
  if (VOCAL_ALIASES.has(key)) return 'vocals'
  if (BASS_ALIASES.has(key)) return 'bass'
  if (DRUM_ALIASES.has(key)) return 'drums'
  if (['melody', 'other', 'others', 'guitar', 'piano', 'instrumental', 'accompaniment'].includes(key)) return 'melody'
  return null
}

export function modelSupportsDjStems(names: readonly string[]): boolean {
  const roles = new Set(names.map(name => djStemRole(name)))
  return roles.has('vocals') && roles.has('bass') && roles.has('drums')
}

export function isStockNamingTemplate(template: unknown, enabled = true): boolean {
  const text = String(template || '').trim()
  return !enabled || STOCK_NAMING_TEMPLATES.has(text)
}
