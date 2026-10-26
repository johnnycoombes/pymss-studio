"""Check channel controls and inline plugin inserts in the real Vue UI with mock IPC."""
import argparse
import ast
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--url', default='http://127.0.0.1:1422')
parser.add_argument('--output-dir', type=Path, required=True)
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)
module = ast.parse(Path(__file__).with_name('editor_vst_ui_smoke.py').read_text('utf-8'))
fixtures = {node.targets[0].id: ast.literal_eval(node.value) for node in module.body
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in ('MOCK', 'SETUP')}
results = []
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, args=['--no-proxy-server'])
    for theme in ('light', 'dark'):
        context = browser.new_context(viewport={'width': 1400, 'height': 900})
        page = context.new_page()
        page.set_default_timeout(15000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        mock = fixtures['MOCK'].replace("themeMode:'light'", 'themeMode:' + repr(theme))
        page.add_init_script(f'({mock})(true)')
        try:
            page.goto(args.url.rstrip('/') + '/#/editor', wait_until='domcontentloaded')
            page.wait_for_load_state('networkidle')
            page.locator('.editor-transport').wait_for(state='visible')
            page.evaluate(fixtures['SETUP'])
            page.evaluate("""() => {
              const slot=(id,name)=>({id,format:'vst3',pluginId:id,name,path:`C:/VST3/${id}.vst3`,bypassed:false,state:'sha256:initial'});
              __editor.session.pluginChain=[slot('master-effect','Master Limiter')];
              __editor.session.tracks[0].pluginChain=[slot('voice-gain','Vocal Gain'),slot('voice-echo','Vocal Echo')];
              __editor.session.tracks.push({...JSON.parse(JSON.stringify(__editor.session.tracks[0])),id:'second',name:'伴奏',pluginChain:[]});
            }""")
            toggle = page.locator('.editor-shell__inspector-toggle')
            if toggle.get_attribute('aria-pressed') == 'false':
                toggle.click()
            inspector = page.locator('.editor-inspector')
            inserts = inspector.locator('.plugin-panel')
            expect(inspector).to_be_visible()
            expect(inserts.locator('.plugin-insert__editor')).to_have_text(['Vocal Gain', 'Vocal Echo'])
            page.evaluate("""() => {
              const playback=document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('editor-playback');
              playback.setTrackLevels({track:[0.5,0.25]}); playback.setMasterLevel([0.8,0.2]);
            }""")
            expect(inspector.get_by_role('meter').first).to_have_attribute('aria-valuetext', '-6.0 dBFS')
            for width, height in ((1400, 900), (980, 680)):
                page.set_viewport_size({'width': width, 'height': height})
                page.wait_for_timeout(150)
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
                assert inspector.bounding_box()['x'] >= 0
                page.screenshot(path=str(args.output_dir / f'{theme}-track-{width}x{height}.png'))
            page.set_viewport_size({'width': 1400, 'height': 900})
            inspector.get_by_role('button', name='主输出', exact=True).click()
            page.wait_for_function('__editor.selectedTrackId === null')
            expect(inserts.locator('.plugin-insert__editor')).to_have_text(['Master Limiter'])
            expect(inspector.get_by_role('meter').first).to_have_attribute('aria-valuetext', '-1.9 dBFS')
            expect(inspector.locator('.inspector-group--effects')).to_have_count(0)
            expect(inspector.locator('.inspector-group--clip')).to_have_count(0)
            master_slider = inspector.locator('.n-slider[aria-label="主音量"] [role="slider"]')
            master_slider.focus()
            master_slider.press('ArrowLeft')
            page.wait_for_function('__editor.session.masterVolume < 1')
            assert page.evaluate('__editor.session.tracks[0].volume') == 1
            page.evaluate('__editor.undo()')
            page.wait_for_function('__editor.session.masterVolume === 1')
            pan_slider = inspector.locator('.n-slider[aria-label="左右平衡"] [role="slider"]')
            pan_slider.press('ArrowRight')
            page.wait_for_function('__editor.session.masterPan > 0')
            assert page.evaluate('__editor.session.tracks[0].pan') == 0
            page.evaluate('__editor.undo()')
            page.wait_for_function('__editor.session.masterPan === 0')
            page.screenshot(path=str(args.output_dir / f'{theme}-master.png'))
            results.append({'scenario': f'{theme}-master-selection-volume-and-undo', 'passed': True})

            # Normal track selection must also select the corresponding insert chain.
            page.evaluate("__editor.selectTrack('track')")
            expect(inserts.locator('.plugin-insert__editor')).to_have_text(['Vocal Gain', 'Vocal Echo'])
            track_slider = inspector.locator('.n-slider[aria-label="音轨音量"] [role="slider"]')
            track_slider.focus()
            track_slider.press('ArrowLeft')
            page.wait_for_function('__editor.session.tracks[0].volume < 1')
            assert page.evaluate('__editor.session.masterVolume') == 1
            page.evaluate('__editor.undo()')
            page.wait_for_function('__editor.session.tracks[0].volume === 1')
            first = inserts.locator('.plugin-insert').first
            first.get_by_role('button', name='打开插件界面：Vocal Gain', exact=True).click()
            page.wait_for_function("__vstCalls.some(c=>c.args?.request?.op==='editor'&&c.args.request.slotId==='voice-gain'&&c.args.request.open)")
            first.get_by_role('button', name='关闭插件界面：Vocal Gain', exact=True).click()
            page.wait_for_function("__vstCalls.some(c=>c.args?.request?.op==='editor'&&c.args.request.slotId==='voice-gain'&&!c.args.request.open)")
            first.get_by_role('switch', name='启用插件：Vocal Gain', exact=True).click()
            page.wait_for_function('__editor.session.tracks[0].pluginChain[0].bypassed')
            assert not page.evaluate('__editor.session.pluginChain[0].bypassed')
            first.get_by_role('button', name='下移插件', exact=True).click()
            page.wait_for_function("__editor.session.tracks[0].pluginChain[1].id === 'voice-gain'")
            inserts.locator('.plugin-insert').last.get_by_role('button', name='移除', exact=True).click()
            page.wait_for_function('__editor.session.tracks[0].pluginChain.length === 1')
            page.evaluate('__editor.undo()')
            page.wait_for_function('__editor.session.tracks[0].pluginChain.length === 2')
            results.append({'scenario': f'{theme}-track-native-editor-bypass-reorder-remove-and-undo', 'passed': True})

            inserts.get_by_role('button', name='音频插件', exact=True).click()
            manager = page.get_by_role('dialog', name='插件管理', exact=True)
            expect(manager).to_be_visible()
            manager.locator('.plugin-manager__target .n-select').click()
            page.locator('.n-base-select-option').filter(has_text='主输出').click()
            page.wait_for_function('__editor.selectedTrackId === null')
            expect(inserts.locator('.plugin-insert__editor')).to_have_text(['Master Limiter'])
            manager.press('Escape')
            expect(manager).not_to_be_visible()
            page.evaluate("__editor.selectTrack('second')")
            expect(inserts.locator('.plugin-insert')).to_have_count(0)
            expect(inserts.get_by_role('button', name='添加音频效果器', exact=True)).to_be_visible()
            page.wait_for_function('__savedVstProjects.length > 0')
            assert not errors, errors
            results.append({'scenario': f'{theme}-library-target-follows-channel-empty-state-and-autosave', 'pageErrors': errors})
        except Exception:
            page.screenshot(path=str(args.output_dir / f'{theme}-failure.png'))
            (args.output_dir / f'{theme}-failure.html').write_text(page.content(), encoding='utf-8')
            raise
        finally:
            context.close()
    browser.close()
(args.output_dir / 'report.json').write_text(json.dumps({'results': results, 'limitations': 'Vue UI with mock IPC; no native DSP or hardware is exercised.'}, indent=2), encoding='utf-8')
print(json.dumps({'passed': len(results)}))
