"""Exercise the actual plugin workbench with mocked IPC; no installed plugin or native window is used."""
import argparse
import ast
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from plugin_catalog_helpers import expand_catalog_groups

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--url', default='http://127.0.0.1:1422')
parser.add_argument('--output-dir', type=Path, required=True)
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)

# Share the existing editor transport fixture without executing its separate smoke run.
module = ast.parse(Path(__file__).with_name('editor_vst_ui_smoke.py').read_text('utf-8'))
fixtures = {
    node.targets[0].id: ast.literal_eval(node.value)
    for node in module.body
    if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
    and node.targets[0].id in ('MOCK', 'SETUP')
}
mock = fixtures['MOCK'].replace('nativeEditor:enabled,experimental:true', 'nativeEditor:enabled,scanControl:true,experimental:true')
mock = mock.replace('window.__pickedDirectory=null;', """window.__pickedDirectory=null; window.__holdScan=false;
  window.__blacklist=[{path:'C:/VST3/Blocked.vst3',name:'Blocked Effect',reason:'timeout',addedAt:1}];
  window.__scanCurrent='C:/VST3/Slow.vst3'; window.__editorError=false; window.__dialogPending=false; window.__dialogPendingResponses=[];""")
mock = mock.replace("if(command==='plugin_host_scan_report') {", """
      if(command==='plugin_host_scan_status') return !window.__scanHeld ? null : {scanId:args.scanId,phase:'scanning',currentPath:__scanCurrent,currentName:__scanCurrent.includes('Slow')?'Slow Effect':'Next Effect',elapsedMs:6200,completed:1,total:3,canSkip:true};
      if(command==='plugin_host_scan_blacklist') return [...__blacklist];
      if(command==='plugin_host_remove_scan_blacklist'){__blacklist=__blacklist.filter(entry=>entry.path!==args.path);return}
      if(command==='plugin_host_skip_scan'){
        if(args.scanId!==window.__scanHeld||args.path!==__scanCurrent)return {skipped:false};
        __blacklist.push({path:__scanCurrent,name:'Slow Effect',reason:'manual',addedAt:Date.now()});
        __scanCurrent='C:/VST3/Next.vst3';return {skipped:true};
      }
      if(command==='plugin_host_cancel_scan'){
        if(args.scanId!==window.__scanHeld)return {cancelled:false};
        window.__cancelled=true;window.__scanRelease?.();return {cancelled:true};
      }
      if(command==='plugin_host_scan_report') {
        window.__cancelled=false;
        if(__holdScan){window.__scanHeld=args.scanId;await new Promise(resolve=>window.__scanRelease=resolve);window.__scanHeld=null;}
""")
mock = mock.replace('return {plugins,failures:[...__scanFailures],scanned:', 'return {plugins,blacklisted:[...__blacklist],cancelled:window.__cancelled,failures:[...__scanFailures],scanned:')
mock = mock.replace('const request=args.request;', """const request=args.request;
        if(__dialogPending){__dialogPendingResponses.push(request.op);throw new Error('PLUGIN_HOST_DIALOG_PENDING: a native plugin dialog is waiting for interaction');}
        if(request.op==='editor'&&__editorError)throw new Error('The plugin editor request timed out');
        if(request.op==='parameters')return {projectId:request.projectId,sessionId:__vstState.sessionId,
          params:Array.from({length:36},(_,index)=>({id:String(index),name:index===0?'Mix':index===1?'High cut':`Control ${index}`,min:index===1?20:0,max:index===1?20000:1,default:0,unit:index===1?'Hz':'None',taper:'Linear',choices:[]})),
          values:Object.fromEntries(Array.from({length:36},(_,index)=>[String(index),index===0?0.019999999552965164:index===1?19999.99999:0.699999988079071]))};
""")


def geometry(page):
    result = page.evaluate("""() => {
      const panel=document.querySelector('.plugin-manager'),rect=panel.getBoundingClientRect();
      const outer=[panel,...panel.querySelectorAll('.plugin-manager__content,.plugin-manager__body,.plugin-manager__library,.plugin-manager__chain')];
      const library=panel.querySelector('.plugin-manager__library').getBoundingClientRect();
      const chain=panel.querySelector('.plugin-manager__chain').getBoundingClientRect();
      return {panel:{x:rect.x,y:rect.y,width:rect.width,height:rect.height},
        outerOverflow:outer.filter(el=>el.scrollHeight>el.clientHeight+1||el.scrollWidth>el.clientWidth+1).map(el=>el.className),
        pageOverflow:document.documentElement.scrollWidth>innerWidth,
        columnRatio:chain.width/library.width,
        parametersInsideSlotList:Boolean(panel.querySelector('.plugin-manager__slots .plugin-parameters'))};
    }""")
    assert not result['outerOverflow'] and not result['pageOverflow'], result
    assert result['columnRatio'] > 1.7, result
    assert not result['parametersInsideSlotList'], result
    rect = result['panel']
    assert rect['x'] >= 0 and rect['y'] >= 0, result
    assert rect['x'] + rect['width'] <= page.viewport_size['width'] + 1, result
    assert rect['y'] + rect['height'] <= page.viewport_size['height'] + 1, result
    return result


def exercise_dialog_waits(page, panel, first, theme, results):
    page.evaluate("async()=>{window.__observedHost=await import('/src/utils/pluginHost.ts')}")
    warning_text = '插件正在等待对话框操作'
    pending_error_toasts = page.locator('.n-message--error-type').filter(has_text=warning_text)
    for operation in ('status', 'editor', 'parameters', 'param'):
        if operation == 'param':
            first.get_by_role('button', name='参数', exact=True).click()
            expect(panel.locator('.plugin-manager__parameter-dock').get_by_role('textbox', name='Mix', exact=True)).to_have_value('0.02')
        session_id = page.evaluate('__observedHost.currentPluginSession().sessionId')
        baseline = page.evaluate("__vstCalls.filter(call=>['load','unload'].includes(call.args?.request?.op)).length")
        marker = f'sha256:dialog-{theme}-{operation}'
        page.evaluate("""marker=>{
          __dialogPending=true;
          const slot=__editor.session.tracks[0].pluginChain[0];
          __vstState.states[slot.id]=marker;__vstState.revision++;
        }""", marker)
        pending_before = page.evaluate('__dialogPendingResponses.length')
        if operation == 'editor':
            first.get_by_role('button', name='打开界面', exact=True).click()
        elif operation == 'parameters':
            first.get_by_role('button', name='参数', exact=True).click()
        elif operation == 'param':
            value = panel.locator('.plugin-manager__parameter-dock').get_by_role('textbox', name='Mix', exact=True)
            value.fill('0.45')
            value.press('Enter')
        else:
            page.wait_for_function('before=>__dialogPendingResponses.slice(before).includes("status")', arg=pending_before)
        page.wait_for_function('({before,operation})=>__dialogPendingResponses.slice(before).includes(operation)', arg={'before': pending_before, 'operation': operation})
        expect(panel.locator('.n-alert')).to_contain_text(warning_text)
        assert panel.locator('.n-alert').evaluate("(el)=>{let vm=el.__vueParentComponent;while(vm){if(vm.type?.name==='Alert')return vm.props.type;vm=vm.parent;}return null}") == 'warning'
        expect(panel.locator('.n-alert')).not_to_contain_text('插件状态保存失败')
        expect(pending_error_toasts).to_have_count(0)
        expect(panel.get_by_role('button', name='重新加载插件', exact=True)).to_have_count(0)
        expect(panel.get_by_role('button', name='重试保存', exact=True)).to_have_count(0)
        assert page.evaluate('__editor.pluginStateUnavailable') is False
        assert page.evaluate('__observedHost.currentPluginSession().unavailable') is False
        assert page.evaluate('__observedHost.currentPluginSession().interactionRequired') is True
        assert page.evaluate('__observedHost.currentPluginSession().sessionId') == session_id
        page.screenshot(path=str(args.output_dir / f'{theme}-dialog-{operation}-waiting.png'))
        status_count = page.evaluate('__dialogPendingResponses.filter(op=>op==="status").length')
        page.wait_for_function('count=>__dialogPendingResponses.filter(op=>op==="status").length>count', arg=status_count)
        expect(pending_error_toasts).to_have_count(0)
        assert page.evaluate('__observedHost.currentPluginSession().unavailable') is False
        assert not page.evaluate('marker=>__savedVstProjects.some(project=>project.tracks[0]?.pluginChain?.[0]?.state===marker)', marker)
        page.evaluate("""() => {
          window.__dialogAlertTransitions=[];
          const record=()=>{
            const alert=document.querySelector('.plugin-manager__error .n-alert');
            let vm=alert?.__vueParentComponent;
            while(vm&&vm.type?.name!=='Alert')vm=vm.parent;
            const type=vm?.props.type||null;
            if(__dialogAlertTransitions.at(-1)?.type!==type)__dialogAlertTransitions.push({type,text:alert?.textContent||null});
          };
          window.__dialogAlertObserver=new MutationObserver(record);
          __dialogAlertObserver.observe(document.querySelector('.plugin-manager__error'),{subtree:true,attributes:true,childList:true,characterData:true});
          record();__dialogPending=false;
        }""")
        page.wait_for_function('!__observedHost.currentPluginSession().interactionRequired')
        expect(panel.locator('.n-alert')).to_have_count(0)
        page.wait_for_function('marker=>__savedVstProjects.some(project=>project.tracks[0]?.pluginChain?.[0]?.state===marker)', arg=marker)
        transitions = page.evaluate('() => {__dialogAlertObserver.disconnect();return __dialogAlertTransitions}')
        assert not any(item['type'] == 'error' for item in transitions), transitions
        assert page.evaluate('__observedHost.currentPluginSession().sessionId') == session_id
        assert page.evaluate('__observedHost.currentPluginSession().unavailable') is False
        assert page.evaluate("__vstCalls.filter(call=>['load','unload'].includes(call.args?.request?.op)).length") == baseline
        expect(panel.get_by_text(warning_text, exact=False)).to_have_count(0)
        expect(pending_error_toasts).to_have_count(0)
        expect(panel.get_by_role('button', name='重新加载插件', exact=True)).to_have_count(0)
        if operation in ('parameters', 'param'):
            panel.get_by_role('button', name='收起参数', exact=True).click()
        page.screenshot(path=str(args.output_dir / f'{theme}-dialog-{operation}-recovered.png'))
        results.append({'scenario': f'{theme}-dialog-{operation}-wait-retains-session-and-autosaves-after-monitor-recovers', 'passed': True})


results = []
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, args=['--no-proxy-server'])
    for theme in ('light', 'dark'):
        context = browser.new_context(viewport={'width': 1400, 'height': 900})
        page = context.new_page()
        page.set_default_timeout(15000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.add_init_script(f'({mock.replace("themeMode:\'light\'", "themeMode:" + repr(theme))})(true)')
        try:
            page.goto(args.url.rstrip('/') + '/#/editor', wait_until='domcontentloaded')
            page.wait_for_load_state('networkidle')
            page.locator('.editor-transport').wait_for(state='visible')
            page.evaluate(fixtures['SETUP'])
            toggle = page.locator('.editor-shell__inspector-toggle')
            if toggle.get_attribute('aria-pressed') == 'false':
                toggle.click()
            sidebar = page.locator('.plugin-panel').get_by_role('button', name='音频插件', exact=True)
            sidebar.click()
            panel = page.get_by_role('dialog', name='插件管理', exact=True)
            expect(panel).to_be_visible()
            expect(panel).to_have_attribute('aria-modal', 'false')
            expect(panel.locator('.plugin-manager__source-manager')).to_have_count(0)
            expect(panel.locator('.plugin-parameters')).to_have_count(0)
            page.evaluate("__pickedDirectory='C:/Audio/VST3'")
            panel.get_by_role('button', name='选择插件目录', exact=True).click()
            expand_catalog_groups(panel)
            expect(panel.locator('.plugin-manager__plugin')).to_have_count(3)
            for name in ('Precision Gain', 'Stereo Echo', 'Precision Gain'):
                panel.locator('.plugin-manager__plugin').filter(has_text=name).click()
                panel.locator('.plugin-manager__add-button').click()
            expect(panel.locator('.plugin-panel__slot')).to_have_count(3)
            expect(panel.locator('.plugin-parameters')).to_have_count(0)
            assert page.evaluate("__vstCalls.filter(call=>call.args?.request?.op==='parameters').length") == 0
            for width, height in ((1400, 900), (980, 680)):
                page.set_viewport_size({'width': width, 'height': height})
                page.wait_for_timeout(150)
                measurements = geometry(page)
                add = panel.locator('.plugin-manager__add-button')
                expect(add).to_be_in_viewport()
                expect(panel.locator('.plugin-panel__slot').first.get_by_role('button', name='打开界面', exact=True)).to_be_in_viewport()
                page.screenshot(path=str(args.output_dir / f'{theme}-chain-{width}x{height}.png'))
                results.append({'scenario': f'{theme}-compact-chain-{width}x{height}', 'geometry': measurements})
                panel.locator('.plugin-panel__slot').first.get_by_role('button', name='参数', exact=True).click()
                dock = panel.locator('.plugin-manager__parameter-dock')
                mix = dock.get_by_role('textbox', name='Mix', exact=True)
                expect(mix).to_have_value('0.02')
                expect(dock.get_by_text('Hz', exact=False)).to_be_visible()
                before = page.evaluate("__vstCalls.filter(call=>call.args?.request?.op==='param').length")
                mix.focus()
                dock.get_by_role('button', name='刷新', exact=True).focus()
                page.wait_for_timeout(100)
                assert page.evaluate("__vstCalls.filter(call=>call.args?.request?.op==='param').length") == before
                assert dock.locator('.plugin-parameters__list').evaluate('(el)=>el.scrollHeight>el.clientHeight')
                geometry(page)
                expect(add).to_be_in_viewport()
                page.screenshot(path=str(args.output_dir / f'{theme}-parameters-{width}x{height}.png'))
                panel.get_by_role('button', name='收起参数', exact=True).click()
            panel.locator('.plugin-panel__slot').first.get_by_role('button', name='参数', exact=True).click()
            mix = panel.locator('.plugin-manager__parameter-dock').get_by_role('textbox', name='Mix', exact=True)
            mix.fill('0.123456789')
            mix.press('Enter')
            page.wait_for_function("__vstCalls.some(call=>call.args?.request?.op==='param'&&call.args.request.value===0.123456789)")
            panel.get_by_role('button', name='收起参数', exact=True).click()
            results.append({'scenario': f'{theme}-parameters-round-display-preserve-edit-and-untouched-blur', 'passed': True})
            sources = panel.get_by_role('button', name='来源与扫描管理', exact=True)
            sources.click()
            manager = panel.locator('.plugin-manager__source-manager')
            expect(manager.locator('.plugin-panel__directory')).to_contain_text('C:/Audio/VST3')
            manager.locator('.plugin-manager__blacklist summary').click()
            expect(manager).to_contain_text('Blocked Effect')
            manager.get_by_role('button', name='将“Blocked Effect”移出黑名单', exact=True).click()
            expect(manager).to_contain_text('扫描黑名单（0）')
            manager.get_by_role('button', name='添加目录', exact=True).focus()
            page.keyboard.press('Escape')
            expect(manager).not_to_be_visible()
            expect(panel).to_be_visible()
            expect(sources).to_be_focused()
            target = panel.locator('.plugin-manager__target .n-select')
            target.click()
            expect(page.locator('.n-base-select-option:visible')).not_to_have_count(0)
            page.keyboard.press('Escape')
            expect(page.locator('.n-base-select-option:visible')).to_have_count(0)
            expect(panel).to_be_visible()
            loop = page.get_by_role('button', name='循环播放', exact=True)
            old_loop = loop.get_attribute('aria-pressed')
            loop.click()
            assert loop.get_attribute('aria-pressed') != old_loop
            second = panel.locator('.plugin-panel__slot').nth(1)
            second.get_by_role('button', name='上移插件', exact=True).click()
            page.wait_for_function("__editor.session.tracks[0].pluginChain[0].pluginId==='echo'")
            first = panel.locator('.plugin-panel__slot').first
            first.get_by_role('checkbox', name='旁路', exact=True).click()
            page.wait_for_function('__editor.session.tracks[0].pluginChain[0].bypassed')
            page.evaluate('__editorError=true')
            first.get_by_role('button', name='打开界面', exact=True).click()
            expect(panel.locator('.n-alert')).to_contain_text('The plugin editor request timed out')
            page.screenshot(path=str(args.output_dir / f'{theme}-editor-error-980x680.png'))
            page.evaluate('__editorError=false')
            first.get_by_role('button', name='打开界面', exact=True).click()
            expect(first.get_by_role('button', name='关闭界面', exact=True)).to_be_visible()
            first.get_by_role('button', name='关闭界面', exact=True).click()
            expect(panel.locator('.n-alert')).to_have_count(0)
            exercise_dialog_waits(page, panel, first, theme, results)
            first.get_by_role('button', name='重新定位', exact=True).click()
            expect(panel.locator('.plugin-manager__plugin')).to_have_count(1)
            panel.locator('.plugin-manager__plugin').click()
            panel.get_by_role('button', name='应用插件位置', exact=True).click()
            expect(panel.locator('.plugin-manager__relocation')).to_have_count(0)
            target.click()
            page.locator('.n-base-select-option').filter(has_text='主输出').click()
            expect(panel.locator('.plugin-panel__slot')).to_have_count(0)
            panel.locator('.plugin-manager__plugin').filter(has_text='Precision Gain').click()
            panel.locator('.plugin-manager__add-button').click()
            page.wait_for_function('__editor.session.pluginChain.length===1')
            assert page.evaluate('__editor.session.tracks[0].pluginChain.length') == 3
            results.append({'scenario': f'{theme}-modeless-keyboard-chain-target-native-errors-and-relocation', 'passed': True})
            page.evaluate("""() => {
              __catalogOverride=Array.from({length:60},(_,index)=>({id:`effect-${index}`,classId:`effect-${index}`,name:`Audio Effect ${index+1}`,vendor:`Vendor ${index%4+1}`,path:`C:/VST3/Effect${index}.vst3`,version:'1',isInstrument:false,category:'Reverb',subCategories:['Fx','Reverb']}));
              const initial=__editor.session.pluginChain[0];
              __editor.setPluginChain(null,Array.from({length:12},(_,index)=>({...initial,id:`dense-slot-${index}`,name:`Processing ${index+1}`})));
            }""")
            panel.get_by_role('button', name='识别插件', exact=True).click()
            expect(panel.locator('.plugin-manager__group-heading')).to_have_count(4)
            expand_catalog_groups(panel)
            expect(panel.locator('.plugin-manager__plugin')).to_have_count(60)
            expect(panel.locator('.plugin-panel__slot')).to_have_count(12)
            panel.locator('.plugin-manager__plugin').first.click()
            geometry(page)
            expect(panel.locator('.plugin-manager__add-button')).to_be_in_viewport()
            assert panel.locator('.plugin-manager__catalog').evaluate('(el)=>el.scrollHeight>el.clientHeight')
            assert panel.locator('.plugin-manager__slots').evaluate('(el)=>el.scrollHeight>el.clientHeight')
            page.screenshot(path=str(args.output_dir / f'{theme}-dense-library-chain-980x680.png'))
            results.append({'scenario': f'{theme}-long-library-and-chain-only-scroll-inside-their-lists', 'passed': True})
            page.evaluate('__holdScan=true')
            panel.get_by_role('button', name='识别插件', exact=True).click()
            progress = panel.locator('.plugin-manager__scan-progress')
            expect(progress).to_contain_text('Slow Effect')
            skip = progress.get_by_role('button', name='跳过并加入黑名单', exact=True)
            stop = progress.get_by_role('button', name='停止扫描', exact=True)
            expect(skip).to_be_enabled()
            expect(stop).to_be_enabled()
            expect(skip).to_be_in_viewport()
            geometry(page)
            page.screenshot(path=str(args.output_dir / f'{theme}-scan-progress-980x680.png'))
            skip.focus()
            skip.press('Enter')
            expect(progress).to_contain_text('Next Effect')
            panel.press('Escape')
            expect(panel).not_to_be_visible()
            assert page.evaluate("__vstCalls.filter(call=>call.command==='plugin_host_cancel_scan').length") == 0
            sidebar.click()
            expect(progress).to_contain_text('Next Effect')
            stop.click()
            expect(progress).to_have_count(0)
            expect(panel).to_contain_text('扫描已停止')
            sources.click()
            manager = panel.locator('.plugin-manager__source-manager')
            manager.locator('.plugin-manager__blacklist summary').click()
            expect(manager).to_contain_text('Slow Effect')
            scans = page.evaluate("__vstCalls.filter(call=>call.command==='plugin_host_scan_report').length")
            manager.get_by_role('button', name='将“Slow Effect”移出黑名单', exact=True).click()
            expect(manager).to_contain_text('扫描黑名单（0）')
            assert page.evaluate("__vstCalls.filter(call=>call.command==='plugin_host_scan_report').length") == scans
            page.screenshot(path=str(args.output_dir / f'{theme}-source-management-980x680.png'))
            manager.get_by_role('button', name='关闭', exact=True).click()
            page.set_viewport_size({'width': 1400, 'height': 900})
            page.wait_for_timeout(150)
            before = panel.bounding_box()
            header = panel.locator('.n-card-header').bounding_box()
            page.mouse.move(header['x'] + 50, header['y'] + 20)
            page.mouse.down()
            page.mouse.move(header['x'] + 85, header['y'] + 35, steps=5)
            page.mouse.up()
            after = panel.bounding_box()
            assert after['x'] - before['x'] > 20 and after['y'] - before['y'] > 10
            panel.press('Escape')
            expect(sidebar).to_be_focused()
            assert not errors, errors
            results.append({'scenario': f'{theme}-scan-control-popup-lifecycle-and-drag', 'pageErrors': errors})
        except Exception:
            details = page.evaluate("() => ({host: window.__observedHost ? {unavailable: __observedHost.currentPluginSession()?.unavailable, interactionRequired: __observedHost.currentPluginSession()?.interactionRequired, sessionId:__observedHost.currentPluginSession()?.sessionId, error:__observedHost.currentPluginSession()?.error}:null, pendingResponses:window.__dialogPendingResponses, alertTransitions:window.__dialogAlertTransitions,alerts:[...document.querySelectorAll('.n-alert')].map(el=>({text:el.textContent, parents:(()=>{const out=[];let vm=el.__vueParentComponent;while(vm){out.push({name:vm.type?.name,type:vm.props?.type});vm=vm.parent;}return out})()})), parameterMessages:[...document.querySelectorAll('.plugin-parameters__message')].map(el=>el.textContent)})")
            (args.output_dir / f'{theme}-failure-state.json').write_text(json.dumps(details, ensure_ascii=False, indent=2), 'utf-8')
            page.screenshot(path=str(args.output_dir / f'{theme}-failure.png'))
            (args.output_dir / f'{theme}-failure-dom.html').write_text(page.content(), 'utf-8')
            raise
        finally:
            context.close()
    context = browser.new_context(viewport={'width': 980, 'height': 680})
    page = context.new_page()
    page.add_init_script(f'({mock})(false)')
    page.goto(args.url.rstrip('/') + '/#/editor', wait_until='networkidle')
    page.evaluate(fixtures['SETUP'])
    toggle = page.locator('.editor-shell__inspector-toggle')
    if toggle.get_attribute('aria-pressed') == 'false':
        toggle.click()
    expect(page.locator('.plugin-panel')).to_have_count(0)
    results.append({'scenario': 'unsupported-platform-stays-hidden', 'passed': True})
    context.close()
    browser.close()
report = {'results': results, 'limitations': 'Actual Vue UI with mocked Tauri IPC. Native binaries, installed plugins, native windows and DSP are not exercised.'}
(args.output_dir / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', 'utf-8')
print(json.dumps({'passed': len(results)}, ensure_ascii=False))
