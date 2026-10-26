import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { isTerminalTaskStatus, taskJobId } from '../src/features/tasks/lifecycle.ts'
import { normalizeStemOutputs } from '../src/utils/stemOutputs.ts'
import { workflowInputNodes } from '../src/workflows/workbench.ts'

const source = ts.createSourceFile('task.ts', readFileSync(new URL('../src/stores/task.ts', import.meta.url), 'utf8'), ts.ScriptTarget.Latest, true)
const names = new Set(['cleanupPath', 'pathKey', 'parentDir', 'samePath', 'cleanupDirChain', 'resultTrashTargets', 'resultTrashEmptyDirs', 'resultTrashTargetsForTasks', 'getRemovalTasks'])
const selected = []
function collect(node) {
  if (ts.isFunctionDeclaration(node) && names.has(node.name?.text)) selected.push(node.getText(source))
  ts.forEachChild(node, collect)
}
collect(source)
assert.equal(selected.length, names.size)
const code = ts.transpileModule(selected.join('\n'), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
const plain = value => Array.from(value)

function task(id, root = '/tmp/results', directory = `${root}/song`, output = `${directory}/vocals.wav`) {
  return { id, jobId: id, status: 'done', input: '/audio/source.wav', jobOutput: root, output: directory,
    outputs: [{ path: output }], files: [output], runConfig: { outputLayout: 'folders' } }
}
function environment(items, windows = false) {
  const state = { tasks: { value: items }, windowsPaths: { value: windows }, isTerminalTaskStatus, taskJobId, workflowInputNodes,
    i18n: { global: { t: key => key } } }
  return vm.runInNewContext(`${code}\n({resultTrashTargets,resultTrashEmptyDirs,resultTrashTargetsForTasks})`, state)
}

test('result file paths preserve trailing whitespace through metadata and recycling selection', () => {
  const path = '/tmp/vocals.wav '
  assert.equal(normalizeStemOutputs([{ path }])[0].path, path)
  const item = task('one', '/tmp', '/tmp', path)
  const store = environment([item])
  assert.deepEqual(plain(store.resultTrashTargets(item)), [path])
  assert.deepEqual(plain(store.resultTrashTargetsForTasks([item]).paths), [path])
})

test('empty directory cleanup stops below POSIX roots with significant spaces or backslashes', () => {
  for (const [root, directory] of [
    ['/tmp/root ', '/tmp/root / song '],
    ['/tmp/results\\', '/tmp/results\\/AC\\DC'],
    ['/tmp/results', '/tmp/results/nested/song'],
  ]) {
    const item = task('one', root, directory)
    const dirs = plain(environment([item]).resultTrashEmptyDirs(item))
    assert.deepEqual(dirs, directory.includes('/nested/') ? [directory, `${root}/nested`] : [directory])
    assert.ok(!dirs.includes(root) && !dirs.includes('/tmp') && !dirs.includes('/'))
  }
})

test('cleanup never ascends outside the recorded output root or through dot segments', () => {
  for (const [root, directory] of [
    ['/tmp/results', '/tmp/archive/song'], ['/tmp/results', '/tmp/results-other/song'],
    ['/tmp/Foo', '/tmp/foo/song'], ['/tmp/results', '/tmp/results/../archive'],
    ['', '/tmp/song'], ['results', 'results/song'], ['/tmp/results', '/tmp/results'],
  ]) {
    const item = task('one', root, directory)
    assert.deepEqual(plain(environment([item]).resultTrashEmptyDirs(item)), [])
  }
})

test('Windows cleanup recognizes drive and UNC boundaries without recycling their roots', () => {
  for (const [root, directory, expected] of [
    ['D:\\', 'D:\\song', ['D:/song']],
    ['D:\\Results', 'd:\\results\\song', ['d:/results/song']],
    ['\\\\?\\D:\\Results', 'D:\\Results\\song', ['D:/Results/song']],
    ['\\\\server\\share\\', '\\\\server\\share\\song', ['//server/share/song']],
    ['\\\\?\\UNC\\server\\share\\', '\\\\server\\share\\song', ['//server/share/song']],
    ['E:\\Outputs', 'E:\\Archive\\song', []],
  ]) {
    const item = task('one', root, directory)
    assert.deepEqual(plain(environment([item], true).resultTrashEmptyDirs(item)), expected)
  }
})

test('removing one result preserves files still referenced by another task', () => {
  const first = task('first')
  const other = task('other')
  const store = environment([first, other])
  assert.deepEqual(plain(store.resultTrashTargetsForTasks([first]).paths), [])
  assert.equal(store.resultTrashTargetsForTasks([first]).retained, 1)
  assert.deepEqual(plain(store.resultTrashTargetsForTasks([first, other]).paths), [first.outputs[0].path])
  assert.equal(store.resultTrashTargetsForTasks([first, other]).retained, 0)
})

test('result recycling protects inputs and includes finished siblings from the same batch', () => {
  const first = task('first')
  first.outputs.push({ path: first.input })
  const sibling = task('sibling', '/tmp/results', '/tmp/results/second')
  sibling.status = 'failed'
  sibling.jobId = first.jobId
  const other = task('other')
  other.outputs = []
  other.input = sibling.outputs[0].path
  const store = environment([first, sibling, other])
  assert.deepEqual(plain(store.resultTrashTargetsForTasks([first]).paths), [first.outputs[0].path])
  other.input = '/audio/other.wav'
  assert.deepEqual(plain(store.resultTrashTargetsForTasks([first]).paths), [first.outputs[0].path, sibling.outputs[0].path])
})

test('POSIX shared-file checks preserve case while Windows recognizes equivalent path spellings', () => {
  const first = task('first', '/tmp', '/tmp', '/tmp/Foo.wav')
  const other = task('other', '/tmp', '/tmp', '/tmp/foo.wav')
  assert.deepEqual(plain(environment([first, other]).resultTrashTargetsForTasks([first]).paths), ['/tmp/Foo.wav'])
  first.outputs[0].path = 'D:\\Results\\Foo.wav'
  other.outputs[0].path = 'd:/results/foo.wav'
  assert.deepEqual(plain(environment([first, other], true).resultTrashTargetsForTasks([first]).paths), [])
  first.outputs[0].path = '\\\\?\\D:\\Results\\Foo.wav'
  assert.deepEqual(plain(environment([first, other], true).resultTrashTargetsForTasks([first]).paths), [])
})

test('shared-file protection recognizes redundant separators and lexical path aliases', () => {
  const first = task('first', '/tmp/results', '/tmp/results', '/tmp/results/parts//../vocals.wav')
  const other = task('other')
  other.outputs[0].path = '/tmp/results/vocals.wav'
  assert.deepEqual(plain(environment([first, other]).resultTrashTargetsForTasks([first]).paths), [])
  first.outputs[0].path = 'D:\\Results\\parts\\..\\Foo.wav'
  other.outputs[0].path = 'd:/results/foo.wav'
  assert.deepEqual(plain(environment([first, other], true).resultTrashTargetsForTasks([first]).paths), [])
})

test('native workflow file and batch-directory inputs protect another task output', () => {
  const first = task('first')
  const consumer = task('consumer')
  consumer.input = ''
  consumer.outputs = []
  for (const [type, value] of [['LoadAudio', first.outputs[0].path], ['pymss_load_audio_batch', '/tmp/results']]) {
    consumer.runConfig.workflowDefinition = { nodes: [{ id: 1, type, widgets_values: [value] }], links: [] }
    const store = environment([first, consumer])
    assert.deepEqual(plain(store.resultTrashTargetsForTasks([first]).paths), [])
    assert.equal(store.resultTrashTargetsForTasks([first]).retained, 1)
    if (type === 'pymss_load_audio_batch') {
      assert.deepEqual(plain(store.resultTrashTargetsForTasks([first, consumer]).paths), [first.outputs[0].path])
    }
  }
})
