"""Check the first-separation guide with isolated browser settings and mocked runtimes."""
import argparse
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--url', default='http://localhost:1420')
parser.add_argument('--features-only', action='store_true', help='Only check the feature tour after the core separation steps')
parser.add_argument('--output-dir', type=Path, default=Path('.tmp/beginner-guide-ui'))
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)

    def new_page(status='pending', platform='win32', backend='cpu', ready=True, model_type='bs_roformer'):
        page = browser.new_page(viewport={'width': 1280, 'height': 850})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.add_init_script('''localStorage.setItem('pymss-studio:app-settings', JSON.stringify({
          startupOnboardingSeen:true,locale:'zh-CN',themeMode:'dark',animationsEnabled:false,
          beginnerGuideProgress:{status:''' + repr(status) + ''',step:0}
        }));''')
        if platform == 'darwin':
            page.add_init_script("Object.defineProperty(navigator,'platform',{value:'MacIntel'})")
        page.goto(args.url)
        page.wait_for_load_state('networkidle')
        page.wait_for_function("document.querySelector('#app')?.__vue_app__?.config.globalProperties.$pinia._s.has('beginnerGuide')")
        page.evaluate('''({platform,backend,ready,modelType})=>{
          const app=document.querySelector('#app').__vue_app__;
          const stores=app.config.globalProperties.$pinia._s;
          window.__guide=stores.get('beginnerGuide');window.__settings=stores.get('settings');
          window.__runtime=stores.get('app');window.__models=stores.get('model');window.__task=stores.get('task');
          window.__router=app.config.globalProperties.$router;window.__ready=ready;
          const info=()=>({platform,ready:__ready,torchBackend:backend,acceleratorAvailable:true,
            installedBackend:backend,packages:{mlx:backend==='mlx'},installedEnvironments:[]});
          __runtime.runtimeInfo=info();__runtime.runtimeInfoError='';__runtime.workerEventConnectionStatus='connected';
          __runtime.ensureRuntimeInfo=__runtime.checkRuntimeInfo=async()=>{__runtime.runtimeInfo=info();return __runtime.runtimeInfo};
          __runtime.checkEnvInBackground=async()=>{};
          __runtime.envInfo={pythonVersion:'3.12',pymssAvailable:true,torchAvailable:backend!=='mlx',mlxAvailable:backend==='mlx',torchBackend:backend,cudaDevices:[]};
          const entry={name:'BS-Roformer',aliases:[],modelType,architecture:modelType,supported:true,
            unsupportedReason:'',category:'MSS',categoryCn:'MSS',primaryCategory:'MSS',primaryCategoryCn:'MSS',
            secondaryCategory:'',secondaryCategoryCn:'',targetStem:'Vocals',configInstruments:'Vocals/Instrumental',
            configTargetInstrument:'Vocals',classificationConfidence:'',classificationBasis:'',sizeBytes:104857600,
            sha256:'',downloaded:true,missingPaths:[],modelPath:'models/model.ckpt',configPath:'models/model.yaml',auxiliaryPaths:[],
            defaultInferenceParams: modelType==='vr'?{window_size:512,aggression:5,batch_size:1,enable_post_process:true,post_process_threshold:0.2,high_end_process:true}:{chunk_size:485100,overlap_size:97020,batch_size:1,standardize:true,normalize:false},
            defaultInferenceParamsResolved:true,defaultInferenceParamsSource:'config'};
          __models.models=[entry];__models.selectedModel=entry.name;__models.modelsLoaded=true;__models.error='';
          __models.loadModels=async()=>{};__models.loadSelectedModelDetail=async()=>{};
          __settings.outputDir='D:/Pymss/Output';__task.separateTemporaryOutputDir='D:/Pymss/Output';
        }''', {'platform': platform, 'backend': backend, 'ready': ready, 'modelType': model_type})
        return page, errors

    def screenshot(page, name):
        page.screenshot(path=str(args.output_dir / name))

    def snapshot(page):
        return page.evaluate('''()=>JSON.stringify({route:location.hash,
          input:__task.inputFiles,output:__settings.outputDir,format:__settings.defaultFormat,
          chunk:__task.chunk_size,overlap:__task.overlap_size,ensemble:__task.ensembleEnabled,
          selected:__models.selectedModel,models:__models.models,preferences:__models.modelPreferences,
          tasks:__task.tasks,workflow:document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('workflow').$state,
          scroll:document.querySelector('.app-content').scrollTop})''')

    def private_workspace(page):
        page.evaluate('''()=>{
          __models.models=[];__models.selectedModel='';
          __task.inputFiles=['Private/original.wav'];__settings.outputDir='Private/Output';
          __settings.defaultFormat='flac';__task.chunk_size=617400;__task.overlap_size=123480;
        }''')
        page.wait_for_timeout(350)
        return snapshot(page)

    def start(page):
        page.get_by_role('button', name='开始指引', exact=True).click()
        expect(page.locator('.tutorial-data-host')).to_be_visible()
        expect(page.locator('.app-body')).to_have_attribute('inert', '')
        child = page.frames[-1]
        expect(child.locator('.beginner-guide-coach')).to_be_visible(timeout=15000)
        child.evaluate('''()=>{
          const app=document.querySelector('#app').__vue_app__;
          const stores=app.config.globalProperties.$pinia._s;
          window.__guide=stores.get('beginnerGuide');window.__task=stores.get('task');
          window.__router=app.config.globalProperties.$router;
        }''')
        return child

    def go(child, step):
        child.evaluate('(id)=>__guide.goTo(id)', step)
        child.wait_for_function('(id)=>__guide.currentStep.id===id&&!__guide.navigating', arg=step)
        expect(child.locator('.beginner-guide-coach, .parameter-field-guide')).to_be_visible()

    def next_step(child):
        child.locator('.beginner-guide-coach, .parameter-field-guide').get_by_role('button').last.click()

    def assert_parameter_guide_spacing(child):
        child.wait_for_function('''()=>{
          const popup=document.querySelector('.parameter-field-guide');
          const modal=document.querySelector('.settings-modal');
          const target=popup&&document.querySelector('[data-parameter-guide="'+popup.dataset.parameter+'"]');
          if(!popup||!modal||!target)return false;
          const a=popup.getBoundingClientRect(),b=modal.getBoundingClientRect(),c=target.getBoundingClientRect();
          const gap=Math.max(c.left-a.right,a.left-c.right,c.top-a.bottom,a.top-c.bottom);
          return a.left>=b.left+14&&a.right<=b.right-14&&a.top>=b.top+14&&a.bottom<=b.bottom-14&&gap>=10;
        }''')

    def assert_drawer_controls(child):
        child.wait_for_function('''()=>{
          const drawer=document.querySelector('.n-drawer');
          const next=document.querySelector('.beginner-guide-coach__actions button:last-child');
          const close=document.querySelector('.n-drawer-header__close');
          if(!drawer||!next||!close||Math.abs(drawer.getBoundingClientRect().right-innerWidth)>1)return false;
          return [next,close].every(element=>{
            const rect=element.getBoundingClientRect();
            return rect.left>=0&&rect.right<=innerWidth&&rect.top>=0&&rect.bottom<=innerHeight
              &&element.contains(document.elementFromPoint(rect.x+rect.width/2,rect.y+rect.height/2));
          });
        }''')

    def exit_and_check(page, before):
        page.locator('.guide-demo-exit').click()
        expect(page.locator('.tutorial-data-host')).to_have_count(0)
        expect(page.locator('.app-body')).not_to_have_attribute('inert', '')
        assert snapshot(page) == before

    page, errors = new_page(ready=False)
    before = private_workspace(page)
    child = start(page)
    # These are the existing software views, including its native sidebar and controls.
    expect(child.locator('.console-topbar')).to_be_visible()
    expect(child.locator('.side-nav .nav-icon').first).to_be_visible()
    expect(child.locator('.guide-demo__console')).to_have_count(0)
    task_count = child.evaluate('__task.tasks.length')
    child.locator('[data-guide="start-separation"]').click(force=True)
    assert child.evaluate('__task.tasks.length') == task_count
    go(child, 'modelChoice')
    expect(child.locator('.model-list-item, .model-card').first).to_be_visible()
    go(child, 'modelManage')
    storage_button = child.locator('[data-guide="model-management"]').get_by_role('button', name='空间管理', exact=True)
    for dismiss in ['close', 'escape', 'mask', 'next']:
        print('Storage dismissal:', dismiss, flush=True)
        storage_button.click()
        expect(child.locator('.n-drawer .storage-row')).to_have_count(3)
        expect(child.locator('.beginner-guide-coach')).to_be_visible()
        assert_drawer_controls(child)
        if dismiss == 'close':
            child.locator('.n-drawer-header__close').click()
        elif dismiss == 'escape':
            child.locator('body').press('Escape')
        elif dismiss == 'mask':
            child.locator('.n-drawer-mask').click(position={'x': 100, 'y': 600})
        else:
            screenshot(page, 'real-ui-storage.png')
            next_step(child)
        expect(child.locator('.n-drawer')).to_have_count(0)
        assert snapshot(page) == before
    child.wait_for_function('__guide.currentStep.id==="input"&&!__guide.navigating')
    child.locator('[data-guide="input"] .picker-btn').first.click()
    child.wait_for_function('__task.inputFiles.length===1')
    assert 'Pymss-Demo' in child.evaluate('__task.inputFiles[0]')
    assert snapshot(page) == before
    screenshot(page, 'real-ui-input.png')

    go(child, 'inference')
    expect(child.locator('.parameter-field-guide')).to_have_attribute('data-parameter', 'tta')
    tta_before = child.evaluate('__task.useTta')
    expect(child.locator('.parameter-field-guide__diagram')).to_be_visible()
    assert_parameter_guide_spacing(child)
    child.locator('.parameter-field-guide .diagram-options button').last.click()
    assert child.evaluate('__task.useTta') == tta_before
    for _ in range(20):
        if child.locator('.parameter-field-guide').get_attribute('data-parameter') == 'overlap_size':
            break
        next_step(child)
    expect(child.locator('.settings-modal')).to_be_visible()
    expect(child.locator('.parameter-field-guide__diagram')).to_be_visible()
    assert_parameter_guide_spacing(child)
    screenshot(page, 'real-ui-parameter-diagram.png')
    next_step(child)
    expect(child.locator('.parameter-field-guide')).to_have_attribute('data-parameter', 'chunk_size')
    child.locator('[data-parameter-guide="chunk_size"] input').fill('529200')
    assert snapshot(page) == before

    go(child, 'run')
    child.locator('[data-guide="start-separation"]').click()
    child.wait_for_function('__task.tasks.some(item=>item.id!=="sample-separation"&&item.status==="done")')
    assert snapshot(page) == before
    go(child, 'results')
    expect(child.locator('.result-row').first).to_be_visible()
    go(child, 'resultEditor')
    child.locator('[data-guide="result-actions"]').first.get_by_role('button').first.click()
    child.wait_for_function('__guide.currentStep.id==="editorPlayback"&&!__guide.navigating')
    expect(child.locator('.beginner-guide-coach')).to_be_visible()
    expect(child.locator('.tutorial-native-window .editor-view')).to_be_visible()
    child.locator('.transport-play').click()
    expect(child.locator('.transport-play')).to_have_attribute('aria-label', '暂停')
    go(child, 'editorMix')
    child.get_by_role('button', name='静音音轨', exact=True).first.click()
    expect(child.get_by_role('button', name='取消静音', exact=True)).to_be_visible()
    screenshot(page, 'real-ui-editor.png')
    go(child, 'editorClips')
    expect(child.locator('.track-row__lane').first).to_be_visible()
    go(child, 'editorExport')
    expect(child.locator('.editor-export-modal')).to_be_visible()
    screenshot(page, 'real-ui-editor-export.png')
    child.locator('.editor-export-modal .export-dialog__footer').get_by_role('button').last.click()
    expect(child.locator('.editor-export-modal')).not_to_be_visible()
    child.wait_for_function('__tutorialRuntime.commands.includes("start_editor_mix_export")')
    assert snapshot(page) == before

    # Follow real workflow controls, including returning through browser history.
    go(child, 'workflowOverview')
    child.get_by_role('button', name='打开编辑器', exact=True).click()
    expect(child.locator('.tutorial-native-window .simple-node--step')).to_be_visible()
    child.wait_for_function('__guide.currentStep.id==="workflowEditor"&&!__guide.navigating')
    child.evaluate('history.back()')
    child.wait_for_function('location.hash==="#/workflows"&&__guide.currentStep.id==="workflowOverview"&&!__guide.navigating')
    expect(child.locator('.beginner-guide-coach')).to_be_visible()
    expect(child.locator('.beginner-guide-preparing-skip')).to_have_count(0)
    screenshot(page, 'real-ui-workflow-return.png')
    child.get_by_role('button', name='打开编辑器', exact=True).click()
    expect(child.locator('.tutorial-native-window .simple-node--step')).to_be_visible()
    child.locator('.simple-inference-settings').click()
    expect(child.locator('.simple-inference-modal')).to_be_visible()
    child.locator('.simple-inference-modal .n-card-header__close').click()
    screenshot(page, 'real-ui-workflow.png')
    go(child, 'workflowOutput')
    expect(child.locator('.simple-workflow-settings-modal')).to_be_visible()
    child.locator('.simple-workflow-settings-modal .n-card-header__close').click()
    expect(child.locator('.beginner-guide-coach')).to_be_visible()
    child.locator('.tutorial-native-window > .n-card-header .n-card-header__close').click()
    child.wait_for_function('__guide.currentStep.id==="workflowOverview"&&!__guide.navigating')
    expect(child.locator('.beginner-guide-coach')).to_be_visible()

    # Creating a workflow through the real chooser must also preserve guide navigation.
    go(child, 'workflowCreate')
    child.locator('[data-guide="workflow-actions"] .workflow-create-button').click()
    expect(child.locator('.workflow-create-chooser')).to_be_visible()
    child.locator('.workflow-create-option').first.click()
    expect(child.locator('.tutorial-native-window .simple-node-editor__topbar')).to_be_visible()
    child.wait_for_function('__router.currentRoute.value.query.new==="1"')
    child.wait_for_function('__guide.currentStep.id==="workflowEditor"&&!__guide.navigating')
    expect(child.locator('.beginner-guide-coach')).to_be_visible()
    next_step(child)
    expect(child.locator('.simple-workflow-settings-modal')).to_be_visible()
    for step in ['toolsOverview', 'paths', 'device', 'downloads', 'runtime', 'help']:
        go(child, step)
        assert snapshot(page) == before, step
    assert child.evaluate('__tutorialRuntime.unknown') == [], child.evaluate('__tutorialRuntime.unknown')
    child.get_by_role('button', name='完成指引', exact=True).click()
    expect(page.locator('.tutorial-data-host')).to_have_count(0)
    assert snapshot(page) == before
    assert not errors, errors
    page.close()

    # A missing target must not remove the tutorial's navigation controls.
    page, errors = new_page()
    child = start(page)
    go(child, 'workflowCreate')
    child.locator('[data-guide="workflow-actions"]').evaluate('(element)=>element.remove()')
    expect(child.locator('.beginner-guide-highlight')).to_have_count(0)
    expect(child.locator('.beginner-guide-coach')).to_be_visible()
    next_step(child)
    child.wait_for_function('__guide.currentStep.id==="workflowOverview"&&!__guide.navigating')
    expect(child.locator('.beginner-guide-coach')).to_be_visible()
    page.locator('.guide-demo-exit').click()
    assert not errors, errors
    page.close()

    # Initialization failures must leave a visible retry/exit path, never an empty frame.
    page, errors = new_page()
    before = private_workspace(page)
    page.evaluate('window.__failTutorialOnce=true')
    page.add_init_script('''if(parent!==window&&location.search.includes('tutorial-frame=1')&&parent.__failTutorialOnce) {
      parent.__failTutorialOnce=false;
      Object.defineProperty(navigator,'mediaDevices',{configurable:false,value:{}});
    }''')
    page.get_by_role('button', name='开始指引', exact=True).click()
    expect(page.locator('.tutorial-data-host__loading')).to_contain_text('新手指引加载失败')
    page.get_by_role('button', name='重新加载', exact=True).click()
    child=page.frames[-1]
    expect(child.locator('.beginner-guide-coach')).to_be_visible(timeout=15000)
    expect(page.locator('.tutorial-data-host__loading')).to_have_count(0)
    exit_and_check(page, before)
    assert not errors, errors
    page.close()

    # Tauri's native bridge is immutable in production. Test SDK relative imports and
    # plugin imports against an immutable trap rather than a replaceable mock invoke.
    for platform, backend in [('win32', 'cpu'), ('darwin', 'mlx')]:
        page, errors = new_page(platform=platform, backend=backend)
        page.add_init_script('''if(parent!==window&&location.search.includes('tutorial-frame=1')) {
          window.__nativeFallbackCalls=0;
          const callbacks=new Map();let sequence=0;
          const internals={metadata:{currentWindow:{label:'main'},currentWebview:{label:'main'}},plugins:{},
            invoke:()=>{__nativeFallbackCalls++;return Promise.reject(new Error('Native IPC escaped'));},
            convertFileSrc:()=>{__nativeFallbackCalls++;throw new Error('Native asset escaped');},
            transformCallback:(handler)=>{callbacks.set(++sequence,handler);return sequence;},
            unregisterCallback:(id)=>callbacks.delete(id),runCallback:(id,data)=>callbacks.get(id)?.(data)};
          Object.defineProperty(window,'__TAURI_INTERNALS__',{value:Object.freeze(internals)});
        }''')
        before = private_workspace(page)
        page.set_viewport_size({'width': 1024, 'height': 680})
        child = start(page)
        go(child, 'modelManage')
        child.locator('[data-guide="model-management"]').get_by_role('button', name='空间管理', exact=True).click()
        expect(child.locator('.n-drawer .storage-row')).to_have_count(3)
        assert_drawer_controls(child)
        screenshot(page, 'real-ui-storage-' + platform + '.png')
        next_step(child)
        expect(child.locator('.n-drawer')).to_have_count(0)
        go(child, 'editorPlayback')
        child.locator('.transport-play').click()
        expect(child.locator('.transport-play')).to_have_attribute('aria-label', '暂停')
        go(child, 'workflowEditor')
        child.evaluate('''async()=>{
          const {getCurrentWindow}=await import('/node_modules/@tauri-apps/api/window.js');
          const {open}=await import('/node_modules/@tauri-apps/plugin-shell/dist-js/index.js');
          try {await getCurrentWindow().minimize()} catch {}
          try {await open('https://example.invalid')} catch {}
        }''')
        assert child.evaluate('__nativeFallbackCalls') == 0
        assert set(child.evaluate('__tutorialRuntime.unknown')) == {'plugin:window|minimize', 'plugin:shell|open'}
        child.evaluate("__tutorialRuntime.invoke('open_workflow_node_editor_window',{payload:{newWorkflow:true}})")
        expect(child.locator('.workflow-node-editor-page')).to_be_visible()
        expect(child.locator('.litegraph-editor')).to_be_visible()
        go(child, 'workflowOutput')
        expect(child.locator('.advanced-output-settings-modal')).to_be_visible()
        assert child.evaluate('__nativeFallbackCalls') == 0
        exit_and_check(page, before)
        assert not errors, errors
        page.close()
    browser.close()
print('Real tutorial views, mock data, native IPC isolation and workspace restoration passed.')
