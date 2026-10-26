import assert from 'node:assert/strict'
import test from 'node:test'
import { pluginViewUrl } from '../src/features/plugin-marketplace/viewUrl.ts'

test('plugin view URLs retain the protocol selected by Tauri', () => {
  for (const base of ['http://pymss-plugin.localhost/view', 'https://pymss-plugin.localhost/view', 'pymss-plugin://localhost/view']) {
    const calls: string[][] = []
    const url = pluginViewUrl('author.chain', 'editor', 'ui/panel #1.html', (path, protocol) => {
      calls.push([path, protocol])
      return base
    })
    assert.deepEqual(calls, [['view', 'pymss-plugin']])
    assert.equal(url, `${base}/author.chain/editor/panel%20%231.html`)
  }
})

test('Windows entry paths request the manifest filename inside its resource directory', () => {
  assert.equal(
    pluginViewUrl('audio-tools', 'panel', 'C:\\plugins\\audio-tools\\ui\\controls.html', () => 'http://pymss-plugin.localhost/view'),
    'http://pymss-plugin.localhost/view/audio-tools/panel/controls.html',
  )
})
