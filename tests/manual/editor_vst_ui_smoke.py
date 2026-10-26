"""Exercise the actual editor plugin UI with a controlled Tauri transport; no native plugin is loaded."""
import argparse
import json
from pathlib import Path
from playwright.sync_api import expect, sync_playwright
from plugin_catalog_helpers import expand_catalog_groups

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--url', default='http://127.0.0.1:1422')
parser.add_argument('--output-dir', type=Path, default=Path('.tmp/vst-ui-smoke'))
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)

MOCK = """enabled => {
  localStorage.setItem('pymss-studio:app-settings', JSON.stringify({startupOnboardingSeen:true,
    beginnerGuideProgress:{status:'skipped',step:0},locale:'zh-CN',themeMode:'light'}));
  localStorage.setItem('pymss-vst-lab-search-paths',JSON.stringify(['E:/Old Development Fixtures/plugins']));
  window.__vstCalls=[]; window.__savedVstProjects=[]; window.__scanError=false; window.__scanDelay=0; window.__scanFailures=[]; window.__missingPaths=[]; window.__catalogOverride=null;
  window.__vstState={sessionId:'ui-native-1',revision:0,states:{},values:{},editors:[],snapshotError:false,statusError:false};
  window.__pickedDirectory=null;
  const callbacks=new Map(),listeners=new Map(); let next=0;
  window.__TAURI_INTERNALS__={
    metadata:{currentWindow:{label:'main'},currentWebview:{label:'main'}},
    transformCallback:callback=>{callbacks.set(++next,callback);return next},
    unregisterCallback:id=>callbacks.delete(id),convertFileSrc:path=>path,
    invoke:async(command,args={})=>{
      __vstCalls.push({command,args:JSON.parse(JSON.stringify(args))});
      if(command==='plugin_host_capabilities') return {enabled,platform:enabled?'windows':'linux',formats:['vst3','vst2'],nativeEditor:enabled,experimental:true,labBuild:true,catalogCache:enabled};
      if(command==='plugin_host_cached_scan_report') {
        const cached=localStorage.getItem('mock-plugin-catalog');
        return cached?JSON.parse(cached):{plugins:[],failures:[],scanned:0,cached:0,hasCache:false};
      }
      if(command==='plugin_host_scan_report') {
        await new Promise(resolve=>setTimeout(resolve,__scanDelay));
        if(__scanError) throw new Error('插件扫描未完成：目录不可访问');
        const plugins=__catalogOverride||[{id:'gain',classId:'gain',name:'Precision Gain',vendor:'Audio Tools',path:'C:/VST3/Gain.vst3',version:'1',isInstrument:false,category:'Eq',subCategories:['Fx','EQ']},
          {id:'echo',classId:'echo',name:'Stereo Echo',vendor:'Room Audio',path:'C:/VST3/Echo.vst3',version:'1',isInstrument:false,category:'Delay',subCategories:['Fx','Delay']},
          {id:'synth',classId:'synth',name:'Keyboard Instrument',vendor:'Audio Tools',path:'C:/VST3/Synth.vst3',version:'1',isInstrument:true,category:'Instrument',subCategories:['Instrument','Synth']}];
        if(args.force)__scanFailures=[];
        const report={plugins,failures:[...__scanFailures],scanned:args.force?3:0,cached:args.force?0:3};
        localStorage.setItem('mock-plugin-catalog',JSON.stringify({...report,scanned:0,cached:plugins.length,hasCache:true}));
        return report;
      }
      if(command==='plugin_host_render') return {path:'C:/Audio/Rendered/mix.wav',duration:40,sampleRate:48000,channels:2,tailLimited:true};
      if(command==='start_editor_mix_export') {
        setTimeout(()=>{for(const listener of listeners.values())if(listener.event==='pymss://worker-event')listener.callback({event:listener.event,id:1,payload:{type:'editor_mix_exported',taskId:args.payload.taskId,payload:{path:'C:/Audio/Exported/mix.wav',duration:40,sampleRate:48000,channels:2,format:'wav'}}})},100);
        return {taskId:args.payload.taskId,started:true};
      }
      if(command==='plugin_host_request') {
        const request=args.request;
        if(request.op==='status'&&__vstState.statusError)throw new Error('The audio host session is closed');
        const slots=request.project?[...(request.project.pluginChain||[]),...request.project.tracks.flatMap(track=>track.pluginChain||[])]:[];
        if((request.op==='load'||request.op==='update')&&slots.some(slot=>!slot.bypassed&&__missingPaths.includes(slot.path)))throw new Error('Plugin file was not found: C:/Missing/Gain.vst3');
        if(request.op==='load'){__vstState.statusError=false;__vstState.states={};__vstState.editors=[]}
        if(request.op==='load'||request.op==='update')for(const slot of [...(request.project.pluginChain||[]),...request.project.tracks.flatMap(track=>track.pluginChain||[])])__vstState.states[slot.id] ||= slot.state||'sha256:initial';
        if(request.op==='load'||request.op==='update'){__vstState.graph=slots;for(const slot of slots)if(slot.bypassed&&__missingPaths.includes(slot.path))delete __vstState.states[slot.id]}
        if(request.op==='snapshot'&&__vstState.snapshotError)throw new Error('Controller state is temporarily unavailable');
        if(request.op==='editor')__vstState.editors=request.open?[request.slotId]:[];
        if(request.op==='parameters')return {projectId:request.projectId,sessionId:__vstState.sessionId,
          params:[{id:'7',name:'Output gain',min:0,max:2,default:1,unit:'Db',choices:[]}],
          values:{'7':__vstState.values[request.slotId]??1}};
        if(request.op==='param'){
          __vstState.values[request.slotId]=request.value;__vstState.revision++;
          return {projectId:request.projectId,sessionId:__vstState.sessionId,value:request.value};
        }
        return {projectId:request.projectId,sessionId:__vstState.sessionId,stateRevision:__vstState.revision,openEditors:__vstState.editors,
          position:0,playing:false,duration:10,sampleRate:48000,unavailablePlugins:(__vstState.graph||[]).filter(slot=>slot.bypassed&&__missingPaths.includes(slot.path)).map(slot=>({slotId:slot.id,message:'Plugin file was not found'})),...(request.op==='snapshot'?{states:{...__vstState.states}}:{}),...(request.op==='editor'?{open:request.open}:{})};
      }
      if(command==='plugin:event|listen') {listeners.set(args.handler,{event:args.event,callback:callbacks.get(args.handler)});return args.handler}
      if(command==='plugin:event|unlisten') {listeners.delete(args.eventId);return}
      if(command==='load_app_store') {try{return JSON.parse(localStorage.getItem('pymss-studio:'+args.name))}catch{return null}}
      if(command==='save_app_store') {localStorage.setItem('pymss-studio:'+args.name,JSON.stringify(args.data));return}
      if(command==='get_build_info') return {version:'0.0.12',variant:'vst-lab',updateSupported:false};
      if(command==='get_runtime_info') return {supported:false,installedEnvironments:[],coreUpdateSupported:false};
      if(command==='load_editor_project')return __savedVstProjects.filter(project=>project.id===args.projectId).at(-1);
      if(command==='save_editor_project'){__savedVstProjects.push(JSON.parse(JSON.stringify(args.project)));return args.project}
      if(command==='pick_input_folder')return __pickedDirectory;
      if(command==='list_editor_projects'||command==='list_models'||command==='list_tasks') return [];
      return null;
    }
  };
}"""
SETUP = """async () => {
  const app=document.querySelector('#app').__vue_app__.config.globalProperties;
  window.__editor=app.$pinia._s.get('editor');
  app.$pinia._s.get('settings').initialized=true;
  __editor.session={id:'vst-ui-project',name:'Audio Session',masterVolume:1,masterPan:0,
    sources:[],tracks:[{id:'track',sourceId:'',role:'stem',name:'人声',volume:1,pan:0,
      muted:false,solo:false,fadeIn:0,fadeOut:0,clips:[]}],createdAt:1,updatedAt:1};
  __editor.selectTrack('track');
  await app.$router.replace({name:'editor',query:{projectId:'vst-ui-project'}});
}"""
GEOMETRY = """() => {
 const panel=document.querySelector('.plugin-manager'), rect=panel.getBoundingClientRect();
 const failures=[];
 for(const el of panel.querySelectorAll('.plugin-manager__content,.plugin-manager__body,.plugin-manager__library,.plugin-manager__chain')){
   if(el.scrollHeight>el.clientHeight+1)failures.push({class:el.className,scrollHeight:el.scrollHeight,clientHeight:el.clientHeight});
 }
 for(const el of panel.querySelectorAll('button,input,.n-select,.plugin-panel__slot')){
   const r=el.getBoundingClientRect();
   if(r.width&&r.height&&(r.left<rect.left-.5||r.right>rect.right+.5)) failures.push({text:el.textContent.trim(),left:r.left,right:r.right});
 }
 return {viewport:{width:innerWidth,height:innerHeight},panel:{left:rect.left,right:rect.right,width:rect.width},
   documentOverflow:document.documentElement.scrollWidth>innerWidth,panelOverflow:rect.left<0||rect.right>innerWidth+.5,failures};
}"""

with sync_playwright() as playwright:
    browser=playwright.chromium.launch(headless=True,args=['--no-proxy-server'])
    results=[]
    for enabled in (True,False):
        context=browser.new_context(viewport={'width':1280,'height':820})
        page=context.new_page()
        page.set_default_timeout(15000)
        errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.add_init_script(f'({MOCK})({json.dumps(enabled)})')
        page.goto(args.url.rstrip('/')+'/#/editor',wait_until='domcontentloaded',timeout=60000)
        page.wait_for_load_state('networkidle',timeout=60000)
        page.locator('.editor-transport').wait_for(state='visible')
        page.evaluate(SETUP)
        toggle=page.locator('.editor-shell__inspector-toggle')
        if toggle.get_attribute('aria-pressed') == 'false':
            toggle.click()
        page.locator('.editor-inspector').wait_for(state='visible')
        panel=page.locator('.plugin-panel')
        if not enabled:
            expect(panel).to_have_count(0)
            page.screenshot(path=str(args.output_dir/'unsupported-platform-1280x820.png'))
            results.append({'scenario':'disabled-platform','panelVisible':False,'pageErrors':errors})
            context.close()
            continue
        expect(panel).to_be_visible()
        expect(panel.get_by_role('button',name='识别插件',exact=True)).to_have_count(0)
        sidebar_height=panel.bounding_box()['height']
        assert sidebar_height < 280,sidebar_height
        expect(panel.get_by_role('button',name='音频插件',exact=True)).to_have_count(1)
        expect(panel.get_by_role('button',name='添加音频效果器',exact=True)).to_have_count(1)
        expect(panel.locator('.n-select')).to_have_count(0)
        page.screenshot(path=str(args.output_dir/'plugin-compact-sidebar-1280x820.png'))
        sidebar=panel
        sidebar.get_by_role('button',name='音频插件',exact=True).click()
        panel=page.get_by_role('dialog',name='插件管理',exact=True)
        expect(panel).to_be_visible()
        expect(panel).to_have_attribute('aria-modal','false')
        expect(page.locator('.n-modal-mask')).to_have_count(0)
        results.append({'scenario':'inline-inserts-empty-state-and-modeless-library','sidebarHeight':sidebar_height})
        print('Plugin manager mounted',flush=True)
        expect(panel.locator('.plugin-panel__directory')).to_have_count(0)
        expect(panel.get_by_role('button',name='识别插件',exact=True)).to_have_count(0)
        expect(panel.locator('.plugin-manager__add-button')).to_have_count(0)
        assert page.evaluate("__vstCalls.filter(c=>c.command==='plugin_host_scan_report').length")==0
        expect(panel.get_by_text('尚未选择插件目录',exact=True)).to_be_visible()
        page.wait_for_timeout(350)
        page.screenshot(path=str(args.output_dir/'plugin-manager-empty-1280x820.png'))
        panel.get_by_role('button',name='选择插件目录',exact=True).click()
        expect(panel.locator('.plugin-panel__directory')).to_have_count(0)
        assert page.evaluate("__vstCalls.filter(c=>c.command==='plugin_host_scan_report').length")==0
        directory='C:/Audio/VST3/Additional Effects and Signal Processing'
        page.evaluate('(path)=>{__pickedDirectory=path}',directory)
        panel.get_by_role('button',name='选择插件目录',exact=True).click()
        panel.get_by_role('button',name='来源与扫描管理',exact=True).click()
        expect(panel.locator('.plugin-panel__directory')).to_contain_text(directory)
        panel.locator('.plugin-manager__source-manager').get_by_role('button',name='关闭',exact=True).click()
        expect(panel.locator('.plugin-manager__source-manager')).not_to_be_visible()
        page.wait_for_function("__vstCalls.some(c=>c.command==='plugin_host_scan_report')")
        assert page.evaluate("__vstCalls.find(c=>c.command==='plugin_host_scan_report').args.paths")==[directory]
        expect(panel.locator('.plugin-manager__group-heading')).to_have_count(2)
        expect(panel.locator('.plugin-manager__plugin')).to_have_count(0)
        first_group=panel.locator('.plugin-manager__group-heading').first
        first_group.focus()
        page.keyboard.press('Space')
        expect(first_group).to_have_attribute('aria-expanded','true')
        expect(panel.locator('.plugin-manager__plugin')).to_have_count(2)
        page.keyboard.press('Enter')
        expect(first_group).to_have_attribute('aria-expanded','false')
        expect(panel.locator('.plugin-manager__plugin')).to_have_count(0)
        catalog_search=panel.get_by_placeholder('搜索名称、厂商、类型或格式',exact=True)
        catalog_search.fill('Stereo Echo')
        expect(panel.locator('.plugin-manager__plugin')).to_have_count(1)
        expect(panel.locator('.plugin-manager__group-heading')).to_have_attribute('aria-expanded','true')
        catalog_search.fill('')
        expect(panel.locator('.plugin-manager__group-heading')).to_have_count(2)
        expect(panel.locator('.plugin-manager__plugin')).to_have_count(0)
        assert page.evaluate("__vstCalls.filter(c=>c.args?.request?.op==='play').length")==0
        page.screenshot(path=str(args.output_dir/'plugin-catalog-collapsed.png'))
        expand_catalog_groups(panel)
        expect(panel.locator('.plugin-manager__plugin')).to_have_count(3)
        results.append({'scenario':'collapsed-catalog-keyboard-toggle-search-expansion-and-restore','passed':True})
        results.append({'scenario':'empty-sources-ignore-legacy-paths-cancel-picker-and-identify-after-selection','passed':True})
        loop=page.get_by_role('button',name='循环播放',exact=True)
        previous_loop=loop.get_attribute('aria-pressed')
        loop.click()
        assert loop.get_attribute('aria-pressed') != previous_loop
        expect(panel).to_be_visible()
        before=panel.bounding_box()
        header=panel.locator('.n-card-header').bounding_box()
        page.mouse.move(header['x']+40,header['y']+24)
        page.mouse.down()
        page.mouse.move(header['x']+72,header['y']+40,steps=8)
        page.mouse.up()
        after=panel.bounding_box()
        page.set_viewport_size({'width':980,'height':680})
        page.wait_for_timeout(200)
        resized=panel.bounding_box()
        assert resized['x']>=0 and resized['y']>=0 and resized['x']+resized['width']<=980.5 and resized['y']+resized['height']<=680.5,resized
        page.set_viewport_size({'width':1280,'height':820})
        page.wait_for_timeout(200)
        assert after['x']-before['x']>20 and after['y']-before['y']>10,{'before':before,'after':after}
        page.screenshot(path=str(args.output_dir/'plugin-manager-dragged-1280x820.png'))
        panel.press('Escape')
        expect(panel).not_to_be_visible()
        expect(sidebar.get_by_role('button',name='音频插件',exact=True)).to_be_focused()
        sidebar.get_by_role('button',name='音频插件',exact=True).click()
        panel.get_by_role('button',name='来源与扫描管理',exact=True).click()
        expect(panel.locator('.plugin-panel__directory')).to_contain_text(directory)
        panel.locator('.plugin-manager__source-manager').get_by_role('button',name='关闭',exact=True).click()
        expect(panel.locator('.plugin-manager__source-manager')).not_to_be_visible()
        assert page.evaluate("__vstCalls.filter(c=>c.command==='plugin_host_scan_report').length")==1
        results.append({'scenario':'drag-background-transport-and-close-reopen-retains-library','passed':True})
        panel.locator('.n-select').first.click()
        expect(page.locator('.n-base-select-option:visible')).not_to_have_count(0)
        page.keyboard.press('Escape')
        expect(page.locator('.n-base-select-option:visible')).to_have_count(0)
        expect(panel).to_be_visible()
        def choose_plugin(name):
            panel.locator('.plugin-manager__plugin').filter(has_text=name).click()
        search=panel.get_by_placeholder('搜索名称、厂商、类型或格式',exact=True)
        expect(panel.locator('.plugin-manager__group-heading strong')).to_have_text(['Audio Tools','Room Audio'])
        choose_plugin('Precision Gain')
        panel.get_by_text('效果类型',exact=True).click()
        expect(panel.get_by_role('radio',name='效果类型',exact=True)).to_be_checked()
        expect(panel.locator('.plugin-manager__group-heading strong')).to_have_count(3)
        expand_catalog_groups(panel)
        assert sorted(panel.locator('.plugin-manager__group-heading strong').all_text_contents())==sorted(['均衡与滤波','延迟','乐器'])
        expect(panel.locator('.plugin-manager__plugin').filter(has_text='Precision Gain')).to_have_attribute('aria-pressed','true')
        search.fill('均衡')
        expect(panel.locator('.plugin-manager__plugin')).to_have_count(1)
        expect(panel.locator('.plugin-manager__group-heading strong')).to_have_text(['均衡与滤波'])
        search.fill('')
        panel.get_by_text('插件格式',exact=True).click()
        expect(panel.locator('.plugin-manager__group-heading strong')).to_have_text(['VST3'])
        panel.get_by_text('厂商',exact=True).click()
        results.append({'scenario':'vendor-effect-type-and-format-groups-preserve-selection-and-search','passed':True})
        choose_plugin('Precision Gain')
        search.fill('not-installed-effect')
        expect(panel.get_by_text('没有符合条件的效果器',exact=True)).to_be_visible()
        expect(panel.locator('.plugin-manager__add-button')).to_be_disabled()
        search.fill('audio tools')
        expect(panel.locator('.plugin-manager__plugin')).to_have_count(2)
        search.fill('PRECISION')
        expect(panel.locator('.plugin-manager__plugin')).to_have_count(1)
        expect(panel.locator('.plugin-manager__plugin')).to_contain_text('Precision Gain')
        search.fill('VST3')
        expect(panel.locator('.plugin-manager__plugin')).to_have_count(3)
        expect(panel.locator('.plugin-manager__plugin').filter(has_text='Keyboard Instrument')).to_be_disabled()
        search.fill('')
        catalog_item=panel.locator('.plugin-manager__plugin').filter(has_text='Stereo Echo')
        catalog_item.press('Enter')
        expect(catalog_item).to_have_attribute('aria-pressed','true')
        assert page.evaluate("__vstCalls.filter(c=>c.args?.request?.op==='play').length")==0
        results.append({'scenario':'searchable-catalog-and-keyboard-selection','passed':True})
        choose_plugin('Precision Gain')
        panel.locator('.plugin-manager__plugin').filter(has_text='Precision Gain').dblclick()
        expect(panel.locator('.plugin-panel__slot')).to_have_count(1)
        page.wait_for_function("__editor.session.tracks[0].pluginChain.length===1")
        expect(panel.locator('.plugin-manager__feedback')).to_contain_text('已将“Precision Gain”添加到“人声”')
        choose_plugin('Stereo Echo')
        panel.locator('.plugin-manager__add-button').click()
        expect(panel.locator('.plugin-panel__slot')).to_have_count(2)
        page.screenshot(path=str(args.output_dir/'plugin-manager-overview-1280x820.png'))
        parameter_slot=panel.locator('.plugin-panel__slot').first
        parameter_slot.get_by_role('button',name='参数',exact=True).click()
        value=panel.locator('.plugin-manager__parameter-dock').get_by_role('textbox',name='Output gain',exact=True)
        value.fill('1.5')
        value.press('Enter')
        page.wait_for_function("__vstCalls.some(c=>c.args?.request?.op==='param'&&c.args.request.value===1.5)")
        page.screenshot(path=str(args.output_dir/'plugin-manager-parameters-1280x820.png'))
        parameter_slot.get_by_role('button',name='参数',exact=True).click()
        results.append({'scenario':'generic-parameters-in-manager','passed':True})
        panel.locator('.plugin-panel__slot').nth(1).get_by_role('button',name='上移插件',exact=True).click()
        page.wait_for_function("__editor.session.tracks[0].pluginChain[0].pluginId==='echo'")
        first=panel.locator('.plugin-panel__slot').first
        first.get_by_role('checkbox',name='旁路',exact=True).click()
        page.wait_for_function("__editor.session.tracks[0].pluginChain[0].bypassed")
        first.get_by_role('button',name='打开界面',exact=True).click()
        expect(first.get_by_role('button',name='关闭界面',exact=True)).to_be_visible()
        page.evaluate("() => { const id=__editor.session.tracks[0].pluginChain[0].id;__vstState.states[id]='sha256:gui-live';__vstState.revision++ }")
        page.wait_for_function("__savedVstProjects.some(project=>project.tracks[0].pluginChain?.[0]?.state==='sha256:gui-live')")
        assert page.evaluate("__vstCalls.filter(c=>c.args?.request?.op==='play').length")==0
        page.evaluate("() => { const id=__editor.session.tracks[0].pluginChain[0].id;__vstState.states[id]='sha256:gui-retry';__vstState.snapshotError=true;__vstState.revision++ }")
        expect(panel.locator('.n-alert')).to_contain_text('Controller state is temporarily unavailable')
        expect(panel.get_by_role('button',name='重试保存',exact=True)).to_be_visible()
        panel.locator('.n-alert').scroll_into_view_if_needed()
        page.wait_for_timeout(300)
        page.screenshot(path=str(args.output_dir/'plugin-state-save-error-1280x820.png'))
        assert page.evaluate('__editor.pluginStateDirty') is True
        page.evaluate('__vstState.snapshotError=false')
        panel.get_by_role('button',name='重试保存',exact=True).click()
        expect(panel.locator('.n-alert')).to_have_count(0)
        page.wait_for_function("__savedVstProjects.some(project=>project.tracks[0].pluginChain?.[0]?.state==='sha256:gui-retry')")
        results.append({'scenario':'native-window-autosave-without-playback-and-failure-retry','passed':True})
        page.evaluate('__vstState.statusError=true')
        expect(panel.get_by_role('button',name='重新加载插件',exact=True)).to_be_visible()
        panel.get_by_role('button',name='重新加载插件',exact=True).click()
        expect(page.get_by_text('是否从工程最近成功保存的状态重新加载插件？原生插件窗口中尚未捕获的修改可能丢失，当前时间线和混音编辑将保留。',exact=True)).to_be_visible()
        confirmation=panel.get_by_role('button',name='确认',exact=True)
        confirmation.focus()
        confirmation.press('Escape')
        expect(confirmation).not_to_be_visible()
        expect(panel).to_be_visible()
        panel.get_by_role('button',name='重新加载插件',exact=True).click()
        previous_loop=page.get_by_role('button',name='循环播放',exact=True).get_attribute('aria-pressed')
        confirmation.focus()
        confirmation.press('Space')
        assert page.get_by_role('button',name='循环播放',exact=True).get_attribute('aria-pressed')==previous_loop
        expect(panel.locator('.n-alert')).to_have_count(0)
        expect(first.get_by_role('button',name='打开界面',exact=True)).to_be_visible()
        first.get_by_role('button',name='打开界面',exact=True).click()
        first.get_by_role('button',name='关闭界面',exact=True).click()
        results.append({'scenario':'explicit-dead-session-reload-with-loss-warning','passed':True})
        assert page.evaluate("__vstCalls.filter(c=>c.command==='plugin_host_request'&&c.args.request.op==='editor').map(c=>c.args.request.open)")==[True,True,False]
        for width,height in ((1280,820),(980,680)):
            page.set_viewport_size({'width':width,'height':height})
            panel.scroll_into_view_if_needed()
            page.wait_for_timeout(200)
            geometry=page.evaluate(GEOMETRY)
            page.screenshot(path=str(args.output_dir/f'plugin-track-chain-{width}x{height}.png'))
            results.append({'scenario':'track-chain','geometry':geometry})
            assert not geometry['documentOverflow'] and not geometry['panelOverflow'] and not geometry['failures'],geometry
        first.get_by_role('button',name='移除',exact=True).click()
        expect(panel.locator('.plugin-panel__slot')).to_have_count(1)
        panel.locator('.n-select').first.click()
        page.locator('.n-base-select-option').filter(has_text='主输出').click()
        expect(panel.locator('.plugin-panel__slot')).to_have_count(0)
        choose_plugin('Stereo Echo')
        panel.locator('.plugin-manager__add-button').click()
        page.wait_for_function("__editor.session.pluginChain.length===1")
        assert page.evaluate("__editor.session.tracks[0].pluginChain.length")==1
        page.screenshot(path=str(args.output_dir/'plugin-master-chain-980x680.png'))
        page.evaluate('__scanDelay=1600')
        panel.get_by_role('button',name='识别插件',exact=True).click()
        expect(panel.locator('.plugin-manager__catalog')).to_have_attribute('aria-busy','true')
        expect(panel.locator('.plugin-manager__add-button')).to_be_enabled()
        page.screenshot(path=str(args.output_dir/'plugin-scan-loading-980x680.png'))
        expect(panel.locator('.plugin-manager__catalog')).to_have_attribute('aria-busy','false')
        page.evaluate('__scanDelay=0;__scanError=true')
        panel.get_by_role('button',name='识别插件',exact=True).click()
        expect(panel.locator('.n-alert')).to_contain_text('插件扫描未完成：目录不可访问')
        expect(panel.locator('.plugin-manager__body')).to_have_attribute('aria-busy','false')
        panel.locator('.n-alert').scroll_into_view_if_needed()
        page.screenshot(path=str(args.output_dir/'plugin-scan-error-980x680.png'))
        previous_scans=page.evaluate("__vstCalls.filter(c=>c.command==='plugin_host_scan_report').length")
        panel.get_by_role('button',name='来源与扫描管理',exact=True).click()
        panel.get_by_role('button',name='移除搜索目录',exact=True).click()
        panel.locator('.plugin-manager__source-manager').get_by_role('button',name='关闭',exact=True).click()
        expect(panel.locator('.plugin-manager__source-manager')).not_to_be_visible()
        expect(panel.locator('.plugin-panel__directory')).to_have_count(0)
        expect(panel.get_by_role('button',name='识别插件',exact=True)).to_have_count(0)
        expect(panel.locator('.plugin-manager__plugin')).to_have_count(0)
        assert page.evaluate("__vstCalls.filter(c=>c.command==='plugin_host_scan_report').length")==previous_scans
        assert page.evaluate("__editor.session.pluginChain.length")==1
        page.evaluate('__scanError=false')
        panel.get_by_role('button',name='选择插件目录',exact=True).click()
        expect(panel.locator('.n-alert')).to_have_count(0)
        results.append({'scenario':'removing-last-source-clears-library-keeps-chain-and-never-scans-defaults','passed':True})
        page.evaluate("__scanFailures=[{path:'C:/Unavailable/Effects.vst3',message:'Module probe timed out'}]")
        panel.get_by_role('button',name='识别插件',exact=True).click()
        panel.locator('.plugin-manager__scan-warning').click()
        panel.get_by_text('查看失败详情',exact=True).click()
        expect(panel.locator('.plugin-panel__scan-failures')).to_contain_text('Module probe timed out')
        panel.get_by_role('button',name='重新检测失败项',exact=True).click()
        expect(panel.locator('.plugin-panel__scan-failures')).to_have_count(0)
        assert page.evaluate("__vstCalls.filter(c=>c.command==='plugin_host_scan_report').at(-1).args.force") is True
        results.append({'scenario':'scan-failure-detail-and-force-reprobe','passed':True})
        page.evaluate("""() => {
          __missingPaths=['C:/Missing/Gain.vst3'];
          const trackSlot=__editor.session.tracks[0].pluginChain[0];
          __vstState.states[trackSlot.id]='sha256:healthy-unflushed';
          __editor.setPluginChain(null,[...__editor.session.pluginChain,{id:'missing-slot',format:'vst3',pluginId:'gain',name:'Precision Gain',path:'C:/Missing/Gain.vst3',bypassed:false,state:'sha256:missing-retained',future:{value:7}}]);
        }""")
        expect(panel.locator('.n-alert')).to_contain_text('Plugin file was not found')
        missing=panel.locator('.plugin-panel__slot').last
        missing.get_by_role('checkbox',name='旁路',exact=True).click()
        page.wait_for_function("__savedVstProjects.at(-1)?.pluginChain?.find(slot=>slot.id==='missing-slot')?.bypassed===true")
        assert page.evaluate("__savedVstProjects.at(-1).pluginChain.find(slot=>slot.id==='missing-slot').state")=='sha256:missing-retained'
        assert page.evaluate("__savedVstProjects.at(-1).tracks[0].pluginChain[0].state")=='sha256:healthy-unflushed'
        expect(missing.locator('.plugin-panel__warning')).to_contain_text('原有状态仍保留')
        missing.get_by_role('button',name='重新定位',exact=True).click()
        expect(panel.locator('.plugin-manager__plugin').filter(has_text='Precision Gain')).to_be_visible()
        expect(panel.locator('.plugin-manager__plugin').filter(has_text='C:/VST3/Gain.vst3')).to_be_visible()
        expect(panel.locator('.plugin-manager__plugin').filter(has_text='Stereo Echo')).to_have_count(0)
        geometry=page.evaluate(GEOMETRY)
        assert not geometry['documentOverflow'] and not geometry['panelOverflow'] and not geometry['failures'],geometry
        page.screenshot(path=str(args.output_dir/'plugin-relocation-menu-980x680.png'))
        choose_plugin('Precision Gain')
        panel.get_by_role('button',name='应用插件位置',exact=True).click()
        page.wait_for_function("__editor.session.pluginChain.find(slot=>slot.id==='missing-slot').path==='C:/VST3/Gain.vst3'")
        relocated=page.evaluate("__editor.session.pluginChain.find(slot=>slot.id==='missing-slot')")
        assert relocated['state']=='sha256:missing-retained' and relocated['future']=={'value':7}
        missing.get_by_role('checkbox',name='旁路',exact=True).click()
        page.wait_for_function("__savedVstProjects.at(-1)?.pluginChain?.find(slot=>slot.id==='missing-slot')?.bypassed===false")
        expect(missing.locator('.plugin-panel__warning')).to_have_count(0)
        page.screenshot(path=str(args.output_dir/'plugin-missing-relinked-980x680.png'))
        results.append({'scenario':'safe-bypass-retains-healthy-gui-and-missing-state-same-class-relocation','passed':True})
        page.get_by_role('button',name='导出混音',exact=True).click()
        page.locator('.editor-export-modal').get_by_role('button',name='导出混音',exact=True).click()
        expect(page.get_by_text('插件尾音已按 30 秒上限截断。导出文件可能未包含完整衰减。',exact=True)).to_be_visible()
        expect(page.locator('.editor-export-modal')).not_to_be_visible()
        page.wait_for_timeout(400)
        page.screenshot(path=str(args.output_dir/'plugin-export-tail-warning-980x680.png'))
        assert page.evaluate('__editor.lastExport.pluginTailLimited') is True
        results.append({'scenario':'export-tail-limit-visible-warning','shown':True})
        page.evaluate("""__catalogOverride=[
          {id:'au:aufx:gain:Test',name:'Gain',vendor:'Vendor',path:'/au',format:'au',auVersion:2},
          {id:'au3:aufx:gain:Test',name:'Gain',vendor:'Vendor',path:'/au3',format:'au',auVersion:3},
          {id:'vst2:one',name:'Shell Gain',vendor:'Vendor',path:'/shell.dll',format:'vst2',isShellChild:true},
          {id:'vst2:two',name:'Shell Gain',vendor:'Vendor',path:'/shell.dll',format:'vst2',isShellChild:true},
          {id:'gain',name:'Gain',vendor:'Vendor',path:'/first/gain.vst3',format:'vst3'},
          {id:'gain',name:'Gain',vendor:'Vendor',path:'/second/gain.vst3',format:'vst3'}];""")
        panel.get_by_role('button',name='识别插件',exact=True).click()
        expect(panel.locator('.plugin-manager__group-heading')).to_have_count(1)
        expand_catalog_groups(panel)
        expect(panel.locator('.plugin-manager__plugin')).to_have_count(6)
        for variant in ['AUv2','AUv3','VST2 Shell']:
            expect(panel.locator('.plugin-manager__catalog .plugin-manager__format').filter(has_text=variant).first).to_be_visible()
        identities=panel.locator('.plugin-manager__plugin-identity')
        for identity in ['vst2:one','vst2:two','/first/gain.vst3','/second/gain.vst3']:
            expect(identities.filter(has_text=identity)).to_be_visible()
        results.append({'scenario':'visible-au-shell-and-duplicate-plugin-identities','passed':True})
        page.evaluate("""__catalogOverride=[
          {id:'vst2:serum',name:'SerumFX',vendor:'Xfer Records',path:'C:/VST2/SerumFX.dll',format:'vst2',isInstrument:false},
          {id:'vst3:serum',name:'SerumFX',vendor:'Xfer Records',path:'C:/VST3/SerumFX.vst3',format:'vst3',isInstrument:false}]""")
        panel.get_by_role('button',name='识别插件',exact=True).click()
        expect(panel.locator('.plugin-manager__group-heading strong')).to_have_text(['Xfer Records'])
        expand_catalog_groups(panel)
        expect(panel.locator('.plugin-manager__plugin')).to_have_count(1)
        expect(panel.locator('.plugin-manager__library-count')).to_have_text('1')
        row=panel.locator('.plugin-manager__plugin')
        expect(panel.locator('.plugin-manager__plugin-row .plugin-manager__format')).to_have_text('VST3 / VST2')
        row.click()
        format_picker=panel.locator('.plugin-manager__format-choice .n-select')
        expect(format_picker).to_contain_text('VST3')
        page.screenshot(path=str(args.output_dir/'plugin-merged-formats-default-vst3.png'))
        count_before_double_click=page.evaluate('__editor.session.pluginChain.length')
        row.dblclick()
        page.wait_for_function("__editor.session.pluginChain.at(-1)?.pluginId==='vst3:serum'")
        assert page.evaluate('__editor.session.pluginChain.length')==count_before_double_click+1
        assert page.evaluate('__editor.session.pluginChain.at(-1).format')=='vst3'
        assert page.evaluate('__editor.session.pluginChain.at(-1).path')=='C:/VST3/SerumFX.vst3'
        format_picker.click()
        page.locator('.n-base-select-option:visible').filter(has_text='VST2').click()
        expect(format_picker).to_contain_text('VST2')
        expect(row).to_have_attribute('aria-pressed','true')
        count_before_double_click=page.evaluate('__editor.session.pluginChain.length')
        row.dblclick()
        page.wait_for_function("__editor.session.pluginChain.at(-1)?.pluginId==='vst2:serum'")
        assert page.evaluate('__editor.session.pluginChain.length')==count_before_double_click+1
        assert page.evaluate('__editor.session.pluginChain.at(-1).format')=='vst2'
        assert page.evaluate('__editor.session.pluginChain.at(-1).path')=='C:/VST2/SerumFX.dll'
        page.screenshot(path=str(args.output_dir/'plugin-merged-formats-explicit-vst2.png'))
        results.append({'scenario':'double-click-adds-once-using-default-vst3-or-explicit-vst2','passed':True})
        page.evaluate("""__catalogOverride=Array.from({length:600},(_,index)=>({
          id:`catalog-${index}`,name:`Catalog Effect ${String(index+1).padStart(4,'0')}`,
          vendor:`Vendor ${index%40+1}`,path:`C:/VST3/Catalog${index}.vst3`,format:'vst3',
          isInstrument:false,category:'Reverb',subCategories:['Fx','Reverb']}))""")
        panel.get_by_role('button',name='识别插件',exact=True).click()
        expect(panel.locator('.plugin-manager__group-heading')).to_have_count(40)
        expect(panel.locator('.plugin-manager__plugin')).to_have_count(0)
        search.fill('Catalog Effect 0599')
        expect(panel.locator('.plugin-manager__group-heading')).to_have_count(1)
        expect(panel.locator('.plugin-manager__plugin')).to_have_count(1)
        expect(panel.locator('.plugin-manager__plugin')).to_contain_text('Catalog Effect 0599')
        search.fill('')
        expect(panel.locator('.plugin-manager__group-heading')).to_have_count(40)
        expect(panel.locator('.plugin-manager__plugin')).to_have_count(0)
        panel.locator('.plugin-manager__group-heading').first.click()
        expect(panel.locator('.plugin-manager__plugin')).to_have_count(15)
        page.screenshot(path=str(args.output_dir/'plugin-catalog-large-library.png'))
        results.append({'scenario':'600-effects-in-40-collapsed-groups-search-and-render-only-open-group','passed':True})
        panel.locator('.plugin-manager__plugin').filter(has_text='Catalog Effect 0001').click()
        expect(panel.locator('.plugin-manager__add-button')).to_be_enabled()
        project_before_refresh=page.evaluate('JSON.stringify(__editor.session)')
        page.evaluate("""__catalogOverride=__catalogOverride.filter(plugin=>!['catalog-0','catalog-598'].includes(plugin.id)).concat([
          {id:'new-install',name:'Newly Installed Effect',vendor:'New Vendor',path:'C:/VST3/New.vst3',format:'vst3',isInstrument:false}])""")
        panel.get_by_role('button',name='识别插件',exact=True).click()
        expect(panel.locator('.plugin-manager__group-heading')).to_have_count(41)
        expect(panel.locator('.plugin-manager__add-button')).to_be_disabled()
        assert page.evaluate('JSON.stringify(__editor.session)')==project_before_refresh
        assert page.evaluate("__vstCalls.filter(call=>call.command==='plugin_host_scan_report').at(-1).args.force") is False
        search.fill('Catalog Effect 0001')
        expect(panel.locator('.plugin-manager__plugin')).to_have_count(0)
        search.fill('Newly Installed Effect')
        expect(panel.locator('.plugin-manager__plugin')).to_have_count(1)
        search.fill('')
        results.append({'scenario':'refresh-removes-old-library-entries-adds-new-plugins-and-retains-project-inserts','passed':True})
        visible_cached_rows = panel.locator('.plugin-manager__plugin').count()
        page.evaluate('__scanDelay=1000')
        panel.get_by_role('button',name='识别插件',exact=True).click()
        page.evaluate("() => {__editor.session={...__editor.session,id:'next-project',pluginChain:[],tracks:[],sources:[]}}")
        expect(panel).not_to_be_visible()
        sidebar.get_by_role('button',name='音频插件',exact=True).click()
        expect(panel.locator('.plugin-manager__body')).to_have_attribute('aria-busy','false')
        expect(panel.locator('.plugin-manager__plugin')).to_have_count(visible_cached_rows)
        expect(panel.locator('.plugin-manager__add-button')).to_be_disabled()
        expect(panel.locator('.plugin-manager__group-heading')).to_have_count(41)
        results.append({'scenario':'project-switch-retains-library-and-discards-late-scan','passed':True})
        assert not errors,errors
        results.append({'scenario':'scan-add-reorder-bypass-remove-native-editor-master-loading-error-recovery','pageErrors':errors})
        context.close()
    browser.close()
    report={'results':results,'limitations':'Browser UI and mocked Tauri messages only. Native binaries, plugin windows, DSP, device output and three-platform compatibility are not exercised.'}
    (args.output_dir/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
