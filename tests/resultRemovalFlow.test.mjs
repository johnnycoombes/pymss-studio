import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { parse } from 'vue/compiler-sfc'

const descriptor = parse(readFileSync(new URL('../src/views/ResultsView.vue', import.meta.url), 'utf8')).descriptor
const source = ts.createSourceFile('results.ts', descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true)
const names = new Set(['removalItems', 'removeResultRecords', 'cleanupLinkedEditorProjects', 'ensureLinkedEditorProjectsClosed',
  'handleRemoveResult', 'handleRemoveSelected', 'handleClearResults', 'groupTaskIds', 'trashEmptyDirs',
  'trashTargetsForItems', 'trashEmptyDirsForItems'])
const statements = source.statements.filter(statement => ts.isFunctionDeclaration(statement) && names.has(statement.name?.text))
assert.equal(statements.length, names.size)
const code = ts.transpileModule(statements.map(statement => statement.getText(source)).join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText

function environment({ open = false, cleanupFailed = false, writeFailed = false, active = false, retained = 0 } = {}) {
  const calls = [], dialogs = [], messages = [], checks = []
  const items = [{ id: 'success' }, { id: 'failed-sibling' }]
  const group = { id: 'run', items: [items[0]] }
  const state = {
    Error, Set, ref: value => ({ value }), NCheckbox: 'checkbox',
    h(type, props) { if (type === 'checkbox') checks.push(props); return {} }, t: key => key,
    selectedResultIds: { value: ['run'] }, selecting: { value: true }, resultGroups: { value: [group] },
    dialog: { warning(options) { dialogs.push(options); options.content() } },
    message: Object.fromEntries(['success', 'warning', 'error'].map(level => [level, message => messages.push([level, message])])),
    editor: {
      async listOpenProjectsForTasks() { calls.push('check-projects'); return open ? ['edit_success'] : [] },
      async deleteProjectsForTasks(ids) {
        calls.push(['projects', Array.from(ids)])
        return { blockedProjectIds: open ? ['edit_success'] : [], failedProjectIds: cleanupFailed ? ['edit_success'] : [] }
      },
    },
    task: {
      getRemovalTasks() { if (active) throw new Error('Batch is running'); return items },
      async removeResults(ids) { calls.push(['records', Array.from(ids)]); if (writeFailed) throw new Error('Disk write failed') },
      resultTrashTargets: item => [`D:/out/${item.id}.wav`], resultTrashEmptyDirs: () => [],
      resultTrashTargetsForTasks: items => ({ paths: items.slice(retained).map(item => `D:/out/${item.id}.wav`), retained }),
      async trashResultPaths(paths) { calls.push(['trash', Array.from(paths)]); return { failed: [] } },
    },
  }
  const view = vm.runInNewContext(`${code}\n({handleRemoveResult,handleRemoveSelected,handleClearResults})`, state)
  return { calls, messages, dialogs, checks, state,
    open(action) { view[action](group) },
    async confirm(deleteFiles = false) {
      checks.at(-1)['onUpdate:checked'](deleteFiles)
      return dialogs.at(-1).onPositiveClick()
    },
  }
}

for (const action of ['handleRemoveResult', 'handleRemoveSelected', 'handleClearResults']) {
  test(`${action} always removes projects and full batch records while retaining unchecked output files`, async () => {
    const env = environment()
    env.open(action)
    await env.confirm()
    assert.deepEqual(env.calls, [['projects', ['success', 'failed-sibling']], ['records', ['success', 'failed-sibling']]])
    assert.ok(env.messages.some(([level]) => level === 'success'))
  })

  test(`${action} recycles outputs only when selected and cleans records afterwards`, async () => {
    const env = environment()
    env.open(action)
    await env.confirm(true)
    assert.deepEqual(env.calls, ['check-projects', ['trash', ['D:/out/success.wav', 'D:/out/failed-sibling.wav']],
      ['projects', ['success', 'failed-sibling']], ['records', ['success', 'failed-sibling']]])
  })

  test(`${action} retains records and files when an editor is open or project cleanup fails`, async () => {
    for (const options of [{ open: true }, { cleanupFailed: true }]) {
      const env = environment(options)
      env.open(action)
      assert.equal(await env.confirm(), false)
      assert.ok(!env.calls.some(call => Array.isArray(call) && ['records', 'trash'].includes(call[0])))
      assert.ok(!env.messages.some(([level]) => level === 'success'))
    }
    const open = environment({ open: true })
    open.open(action)
    assert.equal(await open.confirm(true), false)
    assert.deepEqual(open.calls, ['check-projects'])
  })

  test(`${action} reports record persistence failure without reporting file recycling failure`, async () => {
    const env = environment({ writeFailed: true })
    env.open(action)
    assert.equal(await env.confirm(), false)
    assert.deepEqual(env.messages, [['error', 'Disk write failed']])
  })

  test(`${action} reports protected files while removing the result records`, async () => {
    for (const retained of [1, 2]) {
      const env = environment({ retained })
      env.open(action)
      await env.confirm(true)
      assert.ok(env.calls.some(call => Array.isArray(call) && call[0] === 'records'))
      const trash = env.calls.find(call => Array.isArray(call) && call[0] === 'trash')
      if (retained === 1) assert.deepEqual(trash, ['trash', ['D:/out/failed-sibling.wav']])
      else assert.equal(trash, undefined)
      assert.ok(env.messages.some(([level, key]) => level === 'success' && key === 'results.removeFilesRetained'))
      assert.ok(!env.messages.some(([, key]) => key.startsWith('results.removeNoFiles')))
    }
  })
}

test('an active batch is rejected before opening a deletion dialog', () => {
  const env = environment({ active: true })
  env.open('handleRemoveResult')
  assert.equal(env.dialogs.length, 0)
  assert.deepEqual(env.messages, [['warning', 'Batch is running']])
})
