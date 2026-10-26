import * as native from '@tauri-core-native'
import { isTutorialFrame, tutorialAdapter } from '@/features/onboarding/demoContext'

export * from '@tauri-core-native'
export function invoke<T>(command: string, args?: native.InvokeArgs, options?: native.InvokeOptions): Promise<T> {
  if (isTutorialFrame()) {
    try { return tutorialAdapter().invoke(command, args) as Promise<T> } catch (error) { return Promise.reject(error) }
  }
  return native.invoke<T>(command, args, options)
}
export function convertFileSrc(path: string, protocol?: string) {
  return isTutorialFrame() ? tutorialAdapter().assetUrl(path) : native.convertFileSrc(path, protocol)
}
export function transformCallback<T>(handler?: (value: T) => void, once?: boolean) {
  return isTutorialFrame() ? tutorialAdapter().callback(handler, once) : native.transformCallback(handler, once)
}
export class Resource extends native.Resource {
  override close(): Promise<void> {
    return isTutorialFrame() ? invoke('plugin:resources|close', { rid: this.rid }) : super.close()
  }
}
export class PluginListener extends native.PluginListener {
  override unregister(): Promise<void> {
    return isTutorialFrame() ? invoke(`plugin:${this.plugin}|remove_listener`, { event: this.event, channelId: this.channelId }) : super.unregister()
  }
}
export async function addPluginListener<T>(plugin: string, event: string, handler: (value: T) => void): Promise<native.PluginListener> {
  if (!isTutorialFrame()) return native.addPluginListener(plugin, event, handler)
  const id = tutorialAdapter().callback(handler)
  await invoke(`plugin:${plugin}|register_listener`, { event, handler: id })
  return new PluginListener(plugin, event, id)
}
export function checkPermissions<T>(plugin: string): Promise<T> {
  return isTutorialFrame() ? invoke(`plugin:${plugin}|check_permissions`) : native.checkPermissions(plugin)
}
export function requestPermissions<T>(plugin: string): Promise<T> {
  return isTutorialFrame() ? invoke(`plugin:${plugin}|request_permissions`) : native.requestPermissions(plugin)
}
