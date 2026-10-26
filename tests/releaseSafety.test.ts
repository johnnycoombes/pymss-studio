import assert from 'node:assert/strict'
import test from 'node:test'
import { safeModelName } from '../src/utils/modelName.ts'
import { sameAudioDirectory } from '../src/features/audio-tools/paths.ts'

test('custom model names cannot escape the managed directory on any supported platform', () => {
  for (const name of ['.', '..', '../sibling', '/outside', 'C:\\outside', 'a/b', 'a\\b', 'a:b', 'a*', 'a\u0000b', 'a\u001fb', 'a.', 'CON', 'con.ckpt', 'COM1', 'LPT9.yaml']) {
    assert.equal(safeModelName(name), false, name)
  }
  for (const name of ['my_model', 'model-v2.1', '人声模型', 'COM10', 'LPT10']) assert.equal(safeModelName(name), true, name)
})

test('merge directory comparisons retain POSIX case and normalize Windows paths', () => {
  for (const [first, second] of [['C:\\Music\\', 'c:/music'], ['C:/Music/.', 'C:/Music'], ['\\\\server\\audio\\', '//server/audio'], ['/audio/', '/audio/.'], ['/', '/']]) {
    assert.equal(sameAudioDirectory(first, second), true)
  }
  for (const [first, second] of [['/Audio', '/audio'], ['', ''], ['/input', '/output']]) {
    assert.equal(sameAudioDirectory(first, second), false)
  }
})

test('merge directory comparisons preserve literal backslashes in POSIX names', () => {
  assert.equal(sameAudioDirectory('/audio/a\\b', '/audio/a/b'), false)
  assert.equal(sameAudioDirectory('/audio/a\\b', '/audio/A\\b'), false)
  assert.equal(sameAudioDirectory('/audio/C:\\Music', '/audio/c:/music'), false)
  assert.equal(sameAudioDirectory('/audio/a\\b/', '/audio/a\\b/.'), true)
})
