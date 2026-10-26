type ProtocolUrl = (path: string, protocol: string) => string

export function pluginViewUrl(plugin: string, view: string, entry: string, protocolUrl: ProtocolUrl): string {
  const filename = entry.split(/[\\/]/).filter(Boolean).at(-1)
  if (!filename || filename === '.' || filename === '..') throw new Error('Plugin view entry is missing')
  const base = protocolUrl('view', 'pymss-plugin').replace(/\/$/, '')
  return `${base}/${encodeURIComponent(plugin)}/${encodeURIComponent(view)}/${encodeURIComponent(filename)}`
}
