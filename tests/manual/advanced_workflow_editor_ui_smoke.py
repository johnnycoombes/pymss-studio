"""Exercise advanced-editor controls without running a separation model."""

import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument('--url', default='http://127.0.0.1:1448')
parser.add_argument('--output-dir', type=Path, default=Path('.tmp/advanced-polish'))
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)
copy = json.loads(Path('src/i18n/zh-CN.json').read_text(encoding='utf-8'))

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, args=['--no-proxy-server'])
    context = browser.new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    page.set_default_timeout(10000)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.add_init_script('''
      if (!localStorage.getItem('pymss-studio:app-settings')) localStorage.setItem('pymss-studio:app-settings',
        JSON.stringify({startupOnboardingSeen:true,locale:'zh-CN',themeMode:'light',outputDir:'D:/outputs'}));
    ''')
    page.goto(args.url + '/#/workflow-node-editor?new=1')
    page.wait_for_load_state('networkidle')
    page.add_style_tag(content='.title-bar {display:none !important}')
    canvas = page.locator('canvas.lg-canvas')
    toolbar = page.locator('.litegraph-editor > .toolbar')
    expect(canvas).to_be_visible()
    expect(toolbar.get_by_role('button', name=copy['common']['save'], exact=True)).to_have_count(0)
    expect(toolbar.get_by_role('button', name=copy['workflows']['editMeta'], exact=True)).to_have_count(0)
    page.wait_for_function("document.querySelector('canvas.lg-canvas')?.data?.graph.nodes.length===4")
    expect(page.locator('.config-grid')).to_have_count(0)
    canvas.evaluate('''el=>{
      const graph=el.data.graph;graph.beforeChange();
      graph.nodes.find(n=>n.type==='mss_separate').widgets.find(w=>w.name==='device').value='cpu';
      graph.nodes.find(n=>n.type==='pymss_save_audio').widgets.find(w=>w.name==='output_format').value='flac';
      graph.afterChange();
    }''')
    page.wait_for_function("JSON.parse(localStorage.getItem('pymss-studio:workflow-state') || '{}').workflows?.[0]?.definition.nodes.some(n=>n.type==='pymss_save_audio' && n.widgets_values[0]==='flac')")
    expect(page.locator('.workflow-save-status')).to_have_text(copy['workflows']['autoSaveSaved'])
    expect(page.locator('.n-message')).to_have_count(0)
    saved = page.evaluate("JSON.parse(localStorage.getItem('pymss-studio:workflow-state')).workflows[0]")
    assert next(n for n in saved['definition']['nodes'] if n['type'] == 'mss_separate')['widgets_values'][1] == 'cpu'
    assert next(n for n in saved['definition']['nodes'] if n['type'] == 'pymss_save_audio')['widgets_values'][0] == 'flac'
    canvas.evaluate('el=>{window.__savedCanvas=el.data;window.__savedGraph=el.data.graph}')
    position = canvas.evaluate('''el=>{
      const node=el.data.graph.nodes[0];
      return {id:node.id,x:node.pos[0],point:el.data.ds.convertOffsetToCanvas([node.pos[0]+node.size[0]/2,node.pos[1]-14])};
    }''')
    box = canvas.bounding_box()
    x, y = box['x'] + position['point'][0], box['y'] + position['point'][1]
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 35, y + 20, steps=4)
    page.mouse.up()
    page.wait_for_function('''original => JSON.parse(localStorage.getItem('pymss-studio:workflow-state')).workflows[0]
      .definition.nodes.find(node=>node.id===original.id).pos[0] !== original.x''', arg=position)
    expect(page.locator('.workflow-save-status')).to_have_text(copy['workflows']['autoSaveSaved'])
    undo = toolbar.get_by_role('button', name=copy['common']['undo'], exact=True)
    was_disabled = undo.is_disabled()
    canvas.evaluate('el=>{el.data.ds.scale=.4;el.data.ds.offset=[-700,-500];el.data.setDirty(true,true)}')
    toolbar.get_by_role('button', name=copy['workflows']['fitView'], exact=True).click()
    assert canvas.evaluate('el=>el.data.ds.scale>.4')
    assert undo.is_disabled() == was_disabled
    expect(page.locator('.workflow-save-status')).to_have_text(copy['workflows']['autoSaveSaved'])
    page.screenshot(path=str(args.output_dir / 'after-light.png'), animations='disabled')

    output_button = toolbar.get_by_role('button', name=copy['separate']['output'], exact=True)
    output_button.click()
    output_modal = page.locator('.advanced-output-settings-modal')
    output_path = output_modal.locator('input')
    choose_output = output_modal.get_by_role('button', name=copy['workbench']['chooseOutput'], exact=True)
    default_output = output_modal.get_by_role('button', name=copy['workbench']['defaultOutput'], exact=True)
    expect(output_path).to_have_value('D:/outputs')
    expect(default_output).to_be_disabled()
    expect(output_modal.locator('.binding-layout')).to_have_count(0)
    original_definition = page.evaluate("JSON.parse(localStorage.getItem('pymss-studio:workflow-state')).workflows[0].definition")
    page.evaluate('''() => {
      const settings=document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('settings');
      window.__outputSelection='E:/WorkflowOutput';
      settings.pickFolder=async()=>window.__outputSelection;
    }''')
    choose_output.click()
    expect(output_path).to_have_value('E:/WorkflowOutput')
    assert page.evaluate("document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('settings').outputDir") == 'D:/outputs'
    page.evaluate('window.__outputSelection=null')
    choose_output.click()
    expect(choose_output).to_be_enabled()
    expect(output_path).to_have_value('E:/WorkflowOutput')
    default_output.click()
    expect(output_path).to_have_value('D:/outputs')
    expect(default_output).to_be_disabled()
    page.evaluate("window.__outputSelection='E:/WorkflowOutput'")
    choose_output.click()
    expect(output_path).to_have_value('E:/WorkflowOutput')
    assert page.evaluate("JSON.parse(localStorage.getItem('pymss-studio:workflow-state')).workflows[0].definition") == original_definition
    page.screenshot(path=str(args.output_dir / 'output-settings-light.png'), animations='disabled')
    output_modal.get_by_role('button', name=copy['common']['close'], exact=True).click()

    add = toolbar.get_by_role('button', name=copy['workflows']['addNode'], exact=True)
    add.click()
    palette = page.locator('.palette')
    search = palette.locator('.palette-input')
    expect(search).to_be_focused()
    search.fill('PreviewAudio')
    expect(palette.locator('.palette-item')).to_have_count(0)
    expect(palette.get_by_text(copy['workflows']['advancedEditor']['noMatchingNodes'], exact=True)).to_be_visible()
    search.fill('no-node-matches-this-query')
    expect(palette.get_by_text(copy['workflows']['advancedEditor']['noMatchingNodes'], exact=True)).to_be_visible()
    search.press('Escape')
    expect(palette).not_to_be_visible()
    add.click()
    search.fill('StringConstant')
    page.screenshot(path=str(args.output_dir / 'node-search.png'), animations='disabled')
    search.press('Enter')
    expect(palette).not_to_be_visible()
    page.wait_for_function("document.querySelector('canvas.lg-canvas').data.graph.nodes.length===5")
    center = canvas.evaluate('''el=>{
      const node=el.data.graph.nodes.find(n=>n.type==='StringConstant');
      const p=el.data.ds.convertOffsetToCanvas([node.pos[0]+node.size[0]/2,node.pos[1]+node.size[1]/2]);
      return [Math.abs(p[0]-el.clientWidth/2),Math.abs(p[1]-el.clientHeight/2)];
    }''')
    assert max(center) < 2, center
    undo.click()
    page.wait_for_function("document.querySelector('canvas.lg-canvas').data.graph.nodes.length===4")
    toolbar.get_by_role('button', name=copy['common']['redo'], exact=True).click()
    page.wait_for_function("document.querySelector('canvas.lg-canvas').data.graph.nodes.length===5")
    def rename_workflow(value):
        toolbar.locator('.workflow-name-trigger').click()
        name_input = toolbar.locator('.workflow-name-input input')
        name_input.fill(value)
        name_input.press('Enter')
    rename_workflow('Vocal processing')
    canvas.focus()
    page.keyboard.press('Control+s')
    expect(page.locator('.workflow-save-status')).to_have_text(copy['workflows']['autoSaveSaved'])
    page.wait_for_function("JSON.parse(localStorage.getItem('pymss-studio:workflow-state')).workflows[0].name==='Vocal processing'")
    assert canvas.evaluate('el=>el.data===window.__savedCanvas && el.data.graph===window.__savedGraph')
    entries = page.evaluate("JSON.parse(localStorage.getItem('pymss-studio:workflow-state')).workflows")
    assert len(entries) == 1 and entries[0]['id'] == saved['id']
    graph_before_run = canvas.evaluate('el=>JSON.stringify(el.data.graph.serialize())')
    def outline_pixel(node_type):
        return canvas.evaluate('''(el,type)=>{
          el.data.draw(true,true);
          const node=el.data.graph.nodes.find(n=>n.type===type),[x,y,w,h]=node.boundingRect;
          const point=el.data.ds.convertOffsetToCanvas([x-4/el.data.ds.scale,y+h/2]);
          return Array.from(el.data.ctx.getImageData(Math.round(point[0]*devicePixelRatio),Math.round(point[1]*devicePixelRatio),1,1).data);
        }''', node_type)
    baseline_pixel = outline_pixel('mss_separate')
    page.evaluate('''() => {
      const stores=document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s;
      const entry=stores.get('workflow').workflows[0];
      const node=entry.definition.nodes.find(n=>n.type==='mss_separate');
      stores.get('task').tasks=[{id:'advanced-run',jobId:'advanced-run',input:'',model:entry.name,output:'D:/outputs',
        status:'separating',progress:42,createdAt:Date.now(),updatedAt:Date.now(),logs:['[info] Processing audio'],outputs:[],files:[],
        nodeStates:{[node.id]:{status:'running',progress:42}},nodeLogs:{[node.id]:['[info] Processing audio']},
        runConfig:{runMode:'workflow',workflowId:entry.id,workflowDefinition:entry.definition,
          workbench:{version:1,nativeGraph:true,sourceDefinition:entry.definition,sourceUpdatedAt:entry.updatedAt,
            bindings:{inputs:{},outputs:{},primaryInput:'',outputLayout:'flat'},inputs:{},outputs:{}}}}];
    }''')
    expect(toolbar.locator('.run-status')).to_have_count(0)
    expect(toolbar.locator('.workflow-execution-progress')).to_be_visible()
    expect(page.locator('.litegraph-editor .workflow-node-log')).to_have_count(0)
    toolbar.get_by_role('button', name=copy['workbench']['details'], exact=True).click()
    expect(page.locator('.workflow-run-details-modal .log-console')).to_contain_text('Processing audio')
    page.keyboard.press('Escape')
    expect(page.locator('.workflow-run-details-modal')).not_to_be_visible()
    canvas.evaluate('el=>el.data.draw(true,true)')
    assert canvas.evaluate('el=>JSON.stringify(el.data.graph.serialize())') == graph_before_run
    expect(page.locator('.workflow-save-status')).to_have_text(copy['workflows']['autoSaveSaved'])
    page.screenshot(path=str(args.output_dir / 'running-light.png'), animations='disabled')
    active_pixel = outline_pixel('mss_separate')
    assert active_pixel[1] > active_pixel[0] + 50 and active_pixel[1] > active_pixel[2] + 30, active_pixel
    assert active_pixel != baseline_pixel
    page.evaluate('''() => {
      const task=document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('task').tasks[0];
      const graph=document.querySelector('canvas.lg-canvas').data.graph;
      task.nodeStates[graph.nodes.find(n=>n.type==='mss_separate').id]={status:'done',progress:100};
      task.nodeStates[graph.nodes.find(n=>n.type==='pymss_save_audio').id]={status:'running',progress:0};
    }''')
    assert outline_pixel('mss_separate') == baseline_pixel
    save_pixel = outline_pixel('pymss_save_audio')
    assert save_pixel[1] > save_pixel[0] + 50, save_pixel
    assert canvas.evaluate('el=>JSON.stringify(el.data.graph.serialize())') == graph_before_run
    canvas.evaluate("el=>{el.data.graph.nodes.find(n=>n.type==='pymss_save_audio').collapse()}")
    collapsed_pixel = outline_pixel('pymss_save_audio')
    assert collapsed_pixel[1] > collapsed_pixel[0] + 50, collapsed_pixel
    canvas.evaluate("el=>{el.data.graph.nodes.find(n=>n.type==='pymss_save_audio').collapse()}")
    for terminal_status in ['failed', 'cancelled']:
        page.evaluate("status=>{document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('task').tasks[0].status=status}", terminal_status)
        assert outline_pixel('pymss_save_audio') != save_pixel
        page.evaluate("document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('task').tasks[0].status='separating'")
        assert outline_pixel('pymss_save_audio') == save_pixel
    canvas_before_done = canvas.evaluate('el=>{el.data.draw(true,true);return el.toDataURL()}')
    page.evaluate('''() => {
      const task=document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('task').tasks[0];
      task.status='done';task.progress=100;
      for(const state of Object.values(task.nodeStates)) {state.status='done';state.progress=100}
    }''')
    expect(toolbar.locator('.workflow-execution-progress b')).to_have_text('100%')
    # Completion clears the transient outline instead of retaining green bars.
    assert canvas.evaluate('el=>{el.data.draw(true,true);return el.toDataURL()}') != canvas_before_done
    assert outline_pixel('mss_separate') == baseline_pixel
    assert outline_pixel('pymss_save_audio') != save_pixel
    page.screenshot(path=str(args.output_dir / 'completed-light.png'), animations='disabled')
    page.evaluate("document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('settings').themeMode='dark'")
    expect(page.locator('html')).to_have_class('dark-theme')
    page.screenshot(path=str(args.output_dir / 'running-dark.png'), animations='disabled')
    page.set_viewport_size({'width': 1000, 'height': 760})
    assert toolbar.evaluate('el=>el.scrollWidth<=el.clientWidth+1')
    page.screenshot(path=str(args.output_dir / 'running-narrow.png'), animations='disabled')
    output_button.click()
    expect(output_path).to_have_value('E:/WorkflowOutput')
    page.screenshot(path=str(args.output_dir / 'output-settings-dark.png'), animations='disabled')
    output_modal.get_by_role('button', name=copy['common']['close'], exact=True).click()
    assert not errors, errors

    # A failed automatic write retains the live graph and can be retried explicitly.
    page.evaluate('''() => {
      window.__setItem=Storage.prototype.setItem;
      Storage.prototype.setItem=function(key,value) {
        if(key==='pymss-studio:workflow-state') throw new Error('Storage unavailable');
        return window.__setItem.call(this,key,value);
      };
    }''')
    rename_workflow('Updated workflow')
    retry = page.get_by_role('button', name=copy['workflows']['autoSaveFailed'] + ' · ' + copy['common']['retry'], exact=True)
    expect(retry).to_be_visible()
    assert page.evaluate("JSON.parse(localStorage.getItem('pymss-studio:workflow-state')).workflows[0].name") == 'Vocal processing'
    assert canvas.evaluate('el=>el.data.graph===window.__savedGraph')
    page.evaluate('() => { Storage.prototype.setItem=window.__setItem }')
    retry.click()
    page.wait_for_function("JSON.parse(localStorage.getItem('pymss-studio:workflow-state')).workflows[0].name==='Updated workflow'")
    expect(page.locator('.workflow-save-status')).to_have_text(copy['workflows']['autoSaveSaved'])

    # Leaving immediately flushes a pending edit instead of dropping the debounce.
    rename_workflow('Final workflow')
    page.evaluate("document.querySelector('#app').__vue_app__.config.globalProperties.$router.push('/workflows')")
    page.wait_for_url('**/#/workflows')
    assert page.evaluate("JSON.parse(localStorage.getItem('pymss-studio:workflow-state')).workflows[0].name") == 'Final workflow'
    assert not errors, errors
    page.evaluate("id=>document.querySelector('#app').__vue_app__.config.globalProperties.$router.push('/workflow-node-editor?workflowId='+id)", saved['id'])
    expect(canvas).to_be_visible()
    output_button.click()
    expect(output_path).to_have_value('E:/WorkflowOutput')
    assert not errors, errors

    # Unknown nodes remain native placeholders; existing empty graphs remain empty.
    for kind, nodes in [('unknown', [{'id':1,'type':'unavailable_node','pos':[0,0],'size':[200,100],'inputs':[],'outputs':[]},
                                     {'id':2,'type':'pymss_save_audio','pos':[300,0],'size':[200,120],'inputs':[],'outputs':[]}]),
                        ('empty', [])]:
        entry = {'id':kind,'name':kind,'description':'preserve','format':'graph','formatVersion':1,'createdAt':1,'updatedAt':1,
                 'definition':{'nodes':nodes,'links':[],'extra':{'source':'original'}}}
        case_context = browser.new_context(viewport={'width': 1100, 'height': 800})
        fixtures = {'workflow-state': {'workflows': [entry], 'selectedWorkflowId': entry['id']},
                    'app-settings': {'startupOnboardingSeen': True, 'locale': 'zh-CN'}}
        case_context.add_init_script('for(const [key,value] of Object.entries('+json.dumps(fixtures)+')) localStorage.setItem("pymss-studio:"+key,JSON.stringify(value));')
        page = case_context.new_page()
        canvas = page.locator('canvas.lg-canvas')
        page.goto(args.url + '/#/workflow-node-editor?workflowId=' + kind)
        page.wait_for_load_state('networkidle')
        page.add_style_tag(content='.title-bar {display:none !important}')
        page.screenshot(path=str(args.output_dir / ('preserved-'+kind+'.png')), animations='disabled')
        expect(page.locator('.canvas-error')).to_have_count(0)
        assert canvas.evaluate('el=>el.data.graph.nodes.length') == len(nodes)
        if kind == 'unknown':
            assert canvas.evaluate('el=>el.data.graph.nodes[0].type') == 'unavailable_node'
        actual = page.evaluate("JSON.parse(localStorage.getItem('pymss-studio:workflow-state')).workflows[0]")
        assert actual == entry, (kind, actual)
        case_context.close()
    # A restoration exception must surface in the UI without replacing the input graph.
    failure_context = browser.new_context(viewport={'width': 1100, 'height': 800})
    failure_context.add_init_script('localStorage.setItem("pymss-studio:app-settings",JSON.stringify({startupOnboardingSeen:true,locale:"zh-CN"}))')
    page = failure_context.new_page()
    page.goto(args.url + '/#/workflow-node-editor?new=1')
    page.wait_for_load_state('networkidle')
    page.add_style_tag(content='.title-bar {display:none !important}')
    original = page.evaluate('''() => {
      const element=document.querySelector('.litegraph-editor');
      const graph=element.querySelector('canvas').data.graph;
      const input={nodes:[{id:1,type:'pymss_save_audio',inputs:[],outputs:[]}],links:[],extra:{source:'preserved'}};
      const queue=[document.querySelector('#app')._vnode];
      while(queue.length) {
        const vnode=queue.shift();
        if(vnode?.component?.type.__name==='WorkflowNodeEditorLite') {window.__graphEditor=vnode.component;break}
        if(vnode?.component) queue.push(vnode.component.subTree);
        if(Array.isArray(vnode?.children)) queue.push(...vnode.children);
      }
      graph.configure=()=>{throw new Error('Unable to restore node configuration')};
      window.__graphEditor.vnode.props['onUpdate:definition'](input);
      return input;
    }''')
    expect(page.locator('.canvas-error')).to_be_visible()
    expect(page.locator('.toolbar').get_by_role('button', name=copy['common']['save'], exact=True)).to_have_count(0)
    expect(page.locator('.toolbar').get_by_role('button', name=copy['workflows']['runWorkflowAction'], exact=True)).to_be_disabled()
    assert page.evaluate("window.__graphEditor.exposed.snapshotDefinition()") == original
    page.screenshot(path=str(args.output_dir / 'load-error.png'), animations='disabled')
    failure_context.close()
    browser.close()
    print('PASS: toolbar, output settings, viewport, palette, history, save shortcut, renaming, execution feedback, responsive layout and graph preservation')
