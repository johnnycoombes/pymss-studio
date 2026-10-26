import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { computed, reactive, ref } from 'vue'
import { acceptHMRUpdate, createPinia, defineStore, setActivePinia } from 'pinia'
import { BEGINNER_GUIDE_STEPS, BEGINNER_GUIDE_CHAPTERS, normalizeBeginnerGuideProgress } from '../src/features/onboarding/guide.ts'
import { msstOverlapSize, parameterHelpTopics, PARAMETER_HELP_COPY } from '../src/features/inference/parameterHelp.ts'
import { visibleParameterGuides } from '../src/features/onboarding/parameters.ts'

const source = readFileSync(new URL('../src/stores/beginnerGuide.ts', import.meta.url), 'utf8')
const parsed = ts.createSourceFile('beginnerGuide.ts', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
const code = ts.transpileModule(parsed.statements.filter(node => !ts.isImportDeclaration(node)
  && !(ts.isIfStatement(node) && node.expression.getText(parsed) === 'import.meta.hot')).map(node => node.getText(parsed)).join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText

function environment(progress = { status: 'pending', step: 0 }, owner = true, invitationSeen = false) {
  setActivePinia(createPinia())
  const settings = reactive({ beginnerGuideProgress: { ...progress }, beginnerGuideInvitationSeen: invitationSeen })
  return { guide: definition(settings, owner)(), settings }
}

function definition(settings, owner = true) {
  const exports = {}
  vm.runInNewContext(code, {
    exports, defineStore, ref, computed, useSettingsStore: () => settings,
    BEGINNER_GUIDE_STEPS, BEGINNER_GUIDE_CHAPTERS, normalizeBeginnerGuideProgress, isMainTaskOwner: () => owner,
  })
  return exports.useBeginnerGuideStore
}

const indexOf = id => BEGINNER_GUIDE_STEPS.findIndex(step => step.id === id)
const active = (id, parameter) => ({ status: 'active', step: indexOf(id), stepId: id, ...(parameter ? { parameter } : {}) })
const move = (guide, delta = 1) => { guide.navigating = false; guide.move(delta) }

test('new and existing users receive one invitation regardless of legacy guide status', () => {
  for (const status of ['unseen', 'pending', 'active', 'skipped', 'completed']) {
    const progress = { ...active('toolsOverview'), status }
    const { guide, settings } = environment(progress)
    guide.offer()
    assert.equal(guide.phase, 'invitation', status)
    assert.equal(settings.beginnerGuideInvitationSeen, true)
    assert.deepEqual(settings.beginnerGuideProgress, progress)
  }
})

test('an invitation is not repeated after reopening, skipping, or completing the guide', () => {
  for (const status of ['unseen', 'pending', 'active', 'skipped', 'completed']) {
    const { guide } = environment({ status, step: 0 }, true, true)
    guide.offer(); assert.equal(guide.phase, 'closed', status)
  }
  const { guide, settings } = environment()
  guide.offer(); assert.equal(guide.phase, 'invitation')
  guide.end('skipped'); guide.offer()
  assert.equal(settings.beginnerGuideProgress.status, 'skipped')
  assert.equal(guide.phase, 'closed')
})

test('accepting an invitation starts a new tour unless there is unfinished progress', () => {
  for (const status of ['unseen', 'pending', 'skipped', 'completed']) {
    const { guide, settings } = environment({ ...active('help'), status })
    guide.offer(); guide.start()
    assert.equal(guide.currentStep.id, 'workspace', status)
    assert.equal(settings.beginnerGuideProgress.status, 'active')
  }
  const { guide, settings } = environment(active('inference', 'chunk_size'))
  guide.offer(); guide.start()
  assert.equal(guide.currentStep.id, 'inference')
  assert.equal(settings.beginnerGuideProgress.parameter, 'chunk_size')
})

test('manually starting or dismissing the guide consumes the automatic invitation', () => {
  for (const action of [guide => guide.start(true), guide => guide.end('skipped'), guide => guide.end('completed')]) {
    const { guide, settings } = environment()
    action(guide)
    assert.equal(settings.beginnerGuideInvitationSeen, true)
  }
})

test('replay starts at the workspace while saved steps resume by identity', () => {
  const { guide, settings } = environment(active('toolsOverview'))
  guide.offer(); guide.start()
  assert.equal(guide.currentStep.id, 'toolsOverview')
  guide.start(true)
  assert.equal(guide.currentStep.id, 'workspace')
  assert.equal(settings.beginnerGuideProgress.stepId, 'workspace')
  move(guide, -1); assert.equal(guide.step, 0)
  move(guide, 100); assert.equal(guide.currentStep.id, 'help')
  guide.end('completed'); guide.offer()
  assert.equal(guide.phase, 'closed')
})

test('pause and rapid navigation preserve the current step', () => {
  const { guide, settings } = environment(active('input'))
  guide.start(); const request = guide.navigationRequest
  guide.move(1); assert.equal(guide.currentStep.id, 'input')
  guide.pause('runtime'); guide.move(1)
  assert.equal(guide.phase, 'paused')
  assert.equal(guide.pauseReason, 'runtime')
  assert.equal(guide.navigationRequest, request)
  assert.equal(settings.beginnerGuideProgress.stepId, 'input')
  guide.start(); move(guide)
  assert.equal(guide.currentStep.id, 'modelSelection')
  const navigating = guide.navigationRequest
  guide.move(1); guide.move(-1)
  assert.equal(guide.currentStep.id, 'modelSelection')
  assert.equal(guide.navigationRequest, navigating)
})

test('secondary windows cannot offer, start, or dismiss the main guide', () => {
  const { guide, settings } = environment({ status: 'pending', step: 0 }, false)
  guide.offer(); guide.start(true); guide.end('skipped')
  assert.equal(guide.phase, 'closed')
  assert.equal(settings.beginnerGuideProgress.status, 'pending')
  assert.equal(settings.beginnerGuideInvitationSeen, false)
})

test('hot updates add guide state without discarding the running instance', () => {
  const pinia = createPinia()
  const oldDefinition = defineStore('beginnerGuide', () => ({ phase: ref('running'), navigationRequest: ref(2) }))
  const guide = oldDefinition(pinia)
  const settings = reactive({ beginnerGuideProgress: { status: 'active', step: 4 } })
  const hot = { data: {}, invalidate: () => assert.fail('guide instance should be retained') }
  acceptHMRUpdate(oldDefinition, hot)({ useBeginnerGuideStore: definition(settings) })
  assert.equal(guide.phase, 'running')
  assert.equal(guide.navigationRequest, 2)
  assert.equal(guide.parameterStep, true)
  assert.equal(guide.currentStep.id, 'inference')
  guide.start(); assert.equal(guide.navigationRequest, 3)
  assert.equal(settings.beginnerGuideProgress.stepId, 'inference')
  guide.$dispose()
})

test('related pages remain grouped and tools are introduced in one overview', () => {
  assert.equal(new Set(BEGINNER_GUIDE_STEPS.map(step => step.id)).size, BEGINNER_GUIDE_STEPS.length)
  const chapters = [...new Set(BEGINNER_GUIDE_STEPS.map(step => step.chapter))]
  assert.deepEqual(chapters, Object.keys(BEGINNER_GUIDE_CHAPTERS))
  for (const chapter of chapters) {
    const indices = BEGINNER_GUIDE_STEPS.flatMap((step, index) => step.chapter === chapter ? [index] : [])
    assert.equal(indices.at(-1) - indices[0] + 1, indices.length)
  }
  assert.ok(indexOf('modelManage') < indexOf('input'))
  assert.ok(indexOf('results') < indexOf('editorPlayback'))
  assert.ok(indexOf('editorExport') < indexOf('ensemble'))
  assert.ok(indexOf('workflowEditor') < indexOf('workflowOutput'))
  assert.deepEqual(BEGINNER_GUIDE_STEPS.filter(step => step.chapter === 'tools').map(step => step.id), ['toolsOverview'])
})

test('MSST coverage converts to samples with the worker integer convention', () => {
  assert.equal(msstOverlapSize(485100, 1), 0)
  assert.equal(msstOverlapSize(485100, 2), 242550)
  assert.equal(msstOverlapSize(485100, 4), 363825)
  assert.equal(msstOverlapSize(485101, 4), 363826)
  assert.equal(msstOverlapSize(1, 4), null)
  assert.equal(msstOverlapSize(485100, 0), null)
})

test('VR models receive frame and mask help instead of sample-based MSS explanations', () => {
  assert.deepEqual(parameterHelpTopics(' VR '), ['vrWindow', 'vrAggression', 'vrTta', 'batch', 'normalize', 'bitDepth', 'bitRate'])
  assert.deepEqual(parameterHelpTopics('bs_roformer'), ['chunk', 'overlap', 'tta', 'batch', 'standardize', 'normalize', 'bitDepth', 'bitRate'])
  const all = parameterHelpTopics(null)
  assert.deepEqual(all.slice(0, 2), ['chunk', 'overlap'])
  assert.deepEqual(new Set(all), new Set(Object.keys(PARAMETER_HELP_COPY)))
  assert.equal(new Set(all).size, all.length)
})

test('every diagram topic has localized explanations in both languages', () => {
  for (const language of ['zh-CN', 'en']) {
    const messages = JSON.parse(readFileSync(new URL(`../src/i18n/${language}.json`, import.meta.url), 'utf8'))
    for (const copy of Object.values(PARAMETER_HELP_COPY)) for (const key of Object.values(copy)) {
      assert.equal(typeof key.split('.').reduce((value, part) => value?.[part], messages), 'string', `${language}: ${key}`)
    }
  }
})

const mssContext = { modelType: 'bs_roformer', hasModel: true, defaults: { batch_size: 1, overlap_size: 97020, chunk_size: 485100 }, format: 'wav', canSaveDefaults: true }

test('all visible MSS parameters are covered, with optional diagrams for the relevant fields', () => {
  const items = visibleParameterGuides(mssContext)
  assert.deepEqual(items.map(item => item.id), ['tta', 'batch_size', 'overlap_size', 'chunk_size', 'standardize', 'modelDefaults', 'wavBitDepth', 'normalize'])
  assert.deepEqual(items.filter(item => item.diagram).map(item => item.id), ['tta', 'batch_size', 'overlap_size', 'chunk_size', 'standardize', 'wavBitDepth', 'normalize'])
  assert.deepEqual(visibleParameterGuides({ ...mssContext, modelType: 'apollo', defaults: { num_overlap: 4, chunk_size: 1000 } }).map(item => item.id), ['tta', 'chunk_size', 'standardize', 'modelDefaults', 'wavBitDepth', 'normalize'])
})

test('VR-specific controls are explained only when the model exposes them', () => {
  const items = visibleParameterGuides({ ...mssContext, modelType: 'vr', defaults: { batch_size: 1, window_size: 512, aggression: 5, post_process_threshold: .2, enable_post_process: true, high_end_process: true } })
  assert.deepEqual(items.map(item => item.id), ['tta', 'batch_size', 'window_size', 'aggression', 'post_process_threshold', 'enable_post_process', 'high_end_process', 'modelDefaults', 'wavBitDepth', 'normalize'])
  assert.deepEqual(items.filter(item => item.diagram).map(item => item.diagram), ['vrTta', 'batch', 'vrWindow', 'vrAggression', 'bitDepth', 'normalize'])
  assert.deepEqual(visibleParameterGuides({ ...mssContext, hasModel: false, defaults: {}, canSaveDefaults: false }).map(item => item.id), ['tta', 'wavBitDepth'])
})

test('output encoding steps follow the selected format', () => {
  for (const [format, expected] of [['wav', ['wavBitDepth']], ['flac', ['flacBitDepth']], ['mp3', ['mp3BitRate']], ['m4a', ['m4aBitRate', 'm4aCodec']]]) {
    assert.deepEqual(visibleParameterGuides({ ...mssContext, format }).filter(item => item.group === 'output').map(item => item.id), [...expected, 'normalize'])
  }
})

test('parameter steps keep one settings session and traverse every visible control', () => {
  const { guide, settings } = environment(active('naming'))
  const items = visibleParameterGuides(mssContext)
  guide.registerParameters(items); guide.start()
  assert.equal(guide.parameterStep, false)
  move(guide)
  const total = BEGINNER_GUIDE_STEPS.length + items.length - 2
  for (const [index, parameter] of items.entries()) {
    assert.equal(guide.parameterStep, true)
    assert.equal(guide.currentParameter.id, parameter.id)
    assert.equal(guide.progress, `${indexOf('inference') + index + 1} / ${total}`)
    assert.equal(settings.beginnerGuideProgress.parameter, parameter.id)
    move(guide)
  }
  assert.equal(guide.currentStep.id, 'run')
  assert.equal(guide.parameterStep, false)
  for (const parameter of [...items].reverse()) {
    move(guide, -1)
    assert.equal(guide.currentParameter.id, parameter.id)
  }
  move(guide, -1); assert.equal(guide.currentStep.id, 'naming')
  guide.start(); guide.pause(); assert.equal(guide.parameterStep, false)
})

test('format changes move unavailable controls to the next valid output field', () => {
  const { guide, settings } = environment(active('encoding', 'wavBitDepth'))
  guide.registerParameters(visibleParameterGuides(mssContext)); guide.start()
  guide.navigating = false
  guide.registerParameters(visibleParameterGuides({ ...mssContext, format: 'm4a' }))
  assert.equal(guide.currentParameter.id, 'm4aBitRate')
  assert.equal(settings.beginnerGuideProgress.parameter, 'm4aBitRate')
  const request = guide.navigationRequest
  guide.registerParameters(visibleParameterGuides({ ...mssContext, format: 'm4a' }))
  assert.equal(guide.navigationRequest, request)
})

test('all later chapters can be visited and skipped without completing a task', () => {
  const { guide, settings } = environment(active('queue'))
  guide.registerParameters(visibleParameterGuides(mssContext)); guide.start()
  let previous = Number(guide.progress.split('/')[0])
  for (let index = indexOf('results'); index < BEGINNER_GUIDE_STEPS.length; index++) {
    move(guide)
    assert.equal(guide.step, index)
    assert.equal(guide.parameterStep, false)
    const current = Number(guide.progress.split('/')[0])
    assert.equal(current, previous + 1); previous = current
    assert.equal(settings.beginnerGuideProgress.status, 'active')
  }
  const [current,total] = guide.progress.split('/').map(Number)
  assert.equal(current,total)
  guide.end('completed')
  assert.equal(settings.beginnerGuideProgress.status, 'completed')
})

test('skipping a section bypasses its parameter substeps and retains the next chapter', () => {
  const { guide } = environment(active('inference','chunk_size'))
  guide.registerParameters(visibleParameterGuides(mssContext)); guide.start(); guide.navigating = false
  guide.skipChapter()
  assert.equal(guide.currentStep.id, 'results')
  assert.equal(guide.navigating, true)
  guide.skipChapter(); assert.equal(guide.currentStep.id, 'results')
  guide.navigating = false; guide.skipChapter()
  assert.equal(guide.currentStep.id, 'ensemble')
})

test('old detailed tool progress resumes at the new overview on either platform', () => {
  const { guide } = environment(active('toolAsr'))
  guide.setPlatform(true); guide.start()
  assert.equal(guide.currentStep.id,'toolsOverview')
  move(guide); assert.equal(guide.currentStep.id,'paths')
  guide.setPlatform(false); move(guide,-1)
  assert.equal(guide.currentStep.id,'toolsOverview')
})

test('demo mode survives pause and exits without changing workspace stores', () => {
  const { guide } = environment(active('inference','chunk_size'))
  guide.start(); assert.equal(guide.demoActive,true)
  guide.navigating=false; guide.goTo('editorPlayback')
  assert.equal(guide.currentStep.id,'editorPlayback')
  guide.pause(); assert.equal(guide.demoActive,true)
  guide.start(); guide.end('skipped')
  assert.equal(guide.demoActive,false)
})

test('stable identities survive ordering changes and legacy guides migrate by meaning', () => {
  const saved = normalizeBeginnerGuideProgress({ status:'active',step:0,stepId:'runtime' })
  assert.equal(saved.step,indexOf('runtime'))
  assert.equal(saved.stepId,'runtime')
  for (const [step,id] of [[0,'modelChoice'],[1,'input'],[5,'run'],[8,'modelSearch'],[13,'toolsOverview'],[15,'help']]) {
    assert.equal(normalizeBeginnerGuideProgress({ status:'active',step }).stepId,id)
  }
  const chunk = normalizeBeginnerGuideProgress({ status:'active',step:3 })
  assert.equal(chunk.stepId,'inference'); assert.equal(chunk.parameter,'chunk_size')
  const overlap = normalizeBeginnerGuideProgress({ status:'active',step:4 })
  assert.equal(overlap.stepId,'inference'); assert.equal(overlap.parameter,'overlap_size')
  const encoding = normalizeBeginnerGuideProgress({ status:'active',step:4,parameter:'m4aCodec' })
  assert.equal(encoding.stepId,'encoding'); assert.equal(encoding.parameter,'m4aCodec')
  assert.deepEqual(normalizeBeginnerGuideProgress(null),{status:'unseen',step:0,stepId:'workspace'})
  assert.equal(normalizeBeginnerGuideProgress({status:'unknown',step:999}).stepId,'help')
  assert.equal(normalizeBeginnerGuideProgress({status:'active',step:-1}).stepId,'workspace')
  assert.equal(normalizeBeginnerGuideProgress({status:'active',step:1.5}).stepId,'modelChoice')
})


test('native file drops are ignored during demo and restored afterward', async () => {
  const source=readFileSync(new URL('../src/views/SeparateView.vue',import.meta.url),'utf8').split('<script setup lang="ts">')[1].split('</script>')[0]
  const file=ts.createSourceFile('SeparateView.ts',source,ts.ScriptTarget.Latest,true,ts.ScriptKind.TS)
  let callback
  function visit(node) {
    if(ts.isCallExpression(node)&&ts.isPropertyAccessExpression(node.expression)&&node.expression.name.text==='onDragDropEvent')callback=node.arguments[0]
    ts.forEachChild(node,visit)
  }
  visit(file);assert.ok(callback)
  const javascript=ts.transpileModule('('+callback.getText(file)+')',{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText
  let calls=0
  const beginnerGuide={demoActive:true},isDragging={value:true}
  const drop=vm.runInNewContext(javascript,{beginnerGuide,isDragging,task:{addPaths:async()=>{calls++;return 1}},message:{success(){},warning(){}},t:key=>key})
  await drop({payload:{type:'drop',paths:['private.wav']}})
  assert.equal(calls,0);assert.equal(isDragging.value,false)
  beginnerGuide.demoActive=false
  await drop({payload:{type:'drop',paths:['private.wav']}})
  assert.equal(calls,1)
})
