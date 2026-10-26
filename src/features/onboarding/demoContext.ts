export const isTutorialFrame = () => typeof window !== 'undefined'
  && !!window.parent && window.parent !== window && typeof window.location?.search === 'string'
  && new URLSearchParams(window.location.search).get('tutorial-frame') === '1'

export interface TutorialAdapter {
  invoke: (command: string, args?: unknown) => Promise<unknown>
  callback: (handler?: (value: any) => void, once?: boolean) => number
  assetUrl: (path: string) => string
}

let adapter: TutorialAdapter | null = null
export function installTutorialAdapter(value: TutorialAdapter) { adapter = value }
export function tutorialAdapter(): TutorialAdapter {
  if (!adapter) throw new Error('Tutorial data is not ready')
  return adapter
}
