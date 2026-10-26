"""Verify favorite persistence and plugin-manager transport shortcuts in the real Vue UI."""
import argparse
import ast
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--url', default='http://127.0.0.1:1438')
parser.add_argument('--output-dir', type=Path, required=True)
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)
module = ast.parse(Path(__file__).with_name('editor_vst_ui_smoke.py').read_text('utf-8'))
fixtures = {node.targets[0].id: ast.literal_eval(node.value) for node in module.body
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in ('MOCK', 'SETUP')}
plugins = [dict(id='gain', name='Precision Gain', vendor='Audio Tools', path='C:/VST3/Gain.vst3', format='vst3'),
           dict(id='vst2:gain', name='Precision Gain', vendor='Audio Tools', path='C:/VST2/Gain.dll', format='vst2'),
           dict(id='room', name='Stereo Room', vendor='Room Audio', path='C:/VST3/Room.vst3', format='vst3')]
seed = {'plugins': plugins, 'failures': [{'path': 'C:/VST3/Bad.vst3', 'message': 'Unavailable plugin'}],
        'scanned': 0, 'cached': 3, 'hasCache': True}
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, args=['--no-proxy-server'])
    context = browser.new_context(viewport={'width': 1400, 'height': 900})
    page = context.new_page()
    page.set_default_timeout(15000)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.add_init_script(f"""({fixtures['MOCK']})(true);
      localStorage.setItem('pymss-plugin-library-directories-v1',JSON.stringify(['C:/VST3','C:/VST2']));
      localStorage.setItem('mock-plugin-catalog',JSON.stringify({json.dumps(seed)}));
      const originalInvoke=__TAURI_INTERNALS__.invoke; let playing=false; window.__nativeTransportRev=0;
      __TAURI_INTERNALS__.invoke=async(command,args={{}})=>{{
        const result=await originalInvoke(command,args);
        if(command==='plugin_host_request'){{
          if(args.request.op==='play')playing=true;
          if(['pause','load','unload'].includes(args.request.op))playing=false;
          return {{...result,playing,transportOwner:'main',transportRevision:__nativeTransportRev}};
        }}
        return result;
      }};
    """)

    def open_editor():
        page.goto(args.url.rstrip('/') + '/#/editor', wait_until='networkidle')
        page.evaluate(fixtures['SETUP'])
        page.evaluate("""() => {
          __editor.session.sources=[{id:'source',path:'C:/Audio/source.wav',name:'Source',role:'stem',duration:10,
            sampleRate:48000,channels:2,peaks:[0.2,0.4],channelPeaks:[[0.2,0.4],[0.2,0.4]]}];
          __editor.session.tracks[0].sourceId='source';
          __editor.session.tracks[0].clips=[{id:'clip',assetId:'source',start:0,offset:0,duration:10,volume:1,fadeIn:0,fadeOut:0,muted:false}];
          __editor.session.pluginChain=[{id:'master-gain',format:'vst3',pluginId:'gain',name:'Gain',path:'C:/VST3/Gain.vst3',bypassed:false}];
          window.__transport=document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('editor-playback');
        }""")
        toggle = page.locator('.editor-shell__inspector-toggle')
        if toggle.get_attribute('aria-pressed') == 'false':
            toggle.click()
        page.locator('.editor-inspector .plugin-panel').get_by_role('button', name='音频插件', exact=True).click()
        return page.get_by_role('dialog', name='插件管理', exact=True)

    panel = open_editor()
    search = panel.get_by_placeholder('搜索名称、厂商、类型或格式', exact=True)
    search.fill('Precision Gain')
    expect(panel.locator('.plugin-manager__plugin')).to_have_count(1)
    star = panel.get_by_role('button', name='收藏 Precision Gain', exact=True)
    star.click()
    expect(panel.get_by_role('button', name='取消收藏 Precision Gain', exact=True)).to_have_attribute('aria-pressed', 'true')
    assert page.evaluate('__editor.session.pluginChain.length') == 1
    assert page.evaluate("__vstCalls.filter(call=>call.args?.request?.op==='play').length") == 0
    search.fill('')
    panel.get_by_text('收藏', exact=True).click()
    expect(panel.locator('.plugin-manager__plugin')).to_have_count(1)
    expect(panel.locator('.plugin-manager__group-heading')).to_have_count(0)
    page.goto(args.url.rstrip('/') + '/#/editor', wait_until='networkidle')
    page.reload(wait_until='networkidle')
    panel = open_editor()
    panel.get_by_text('收藏', exact=True).click()
    expect(panel.locator('.plugin-manager__plugin')).to_have_count(1)
    row = panel.locator('.plugin-manager__plugin')
    row.focus()
    page.keyboard.down('Space')
    page.wait_for_function("__transport.status==='playing'")
    page.keyboard.down('Space')
    page.keyboard.up('Space')
    assert page.evaluate("__vstCalls.filter(call=>call.args?.request?.op==='play').length") == 1
    row.press('Space')
    page.wait_for_function("__transport.status==='paused'")
    search = panel.get_by_placeholder('搜索名称、厂商、类型或格式', exact=True)
    search.fill('Precision')
    search.press('Space')
    expect(search).to_have_value('Precision ')
    assert page.evaluate("__transport.status") == 'paused'
    panel.get_by_role('button', name='来源与扫描管理', exact=True).click()
    summary = panel.locator('.plugin-manager__source-manager details > summary').first
    summary.focus()
    summary.press('Space')
    assert summary.evaluate('element=>element.parentElement.open')
    assert page.evaluate("__transport.status") == 'paused'
    panel.locator('.plugin-manager__source-manager').get_by_role('button', name='关闭', exact=True).click()
    search.fill('')
    star = panel.get_by_role('button', name='取消收藏 Precision Gain', exact=True)
    star.focus()
    star.press('Space')
    expect(panel.locator('.plugin-manager__plugin')).to_have_count(0)
    expect(panel.get_by_text('暂无收藏插件', exact=True)).to_be_visible()
    assert page.evaluate("__transport.status") == 'paused'
    panel.get_by_text('厂商', exact=True).click()
    search.fill('Precision Gain')
    panel.get_by_role('button', name='收藏 Precision Gain', exact=True).click()
    page.evaluate('__editor.selectTrack(null)')
    panel.get_by_role('button', name='打开界面', exact=True).click()
    page.evaluate('__nativeTransportRev++')
    page.wait_for_function("__transport.status==='playing'")
    page.evaluate('__nativeTransportRev++')
    page.wait_for_function("__transport.status==='paused'")
    for width, height in ((1400, 900), (980, 680)):
        page.set_viewport_size({'width': width, 'height': height})
        page.wait_for_timeout(100)
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
        page.screenshot(path=str(args.output_dir / f'favorites-{width}x{height}.png'))
    assert not errors, errors
    report = {'favoritePersistedAcrossReload': True, 'mergedFormatsShareFavorite': True,
              'spaceTogglesPlayback': True, 'nativeTransportIntentTogglesPlayback': True, 'repeatIgnored': True, 'typingPreserved': True,
              'summaryKeyboardPreserved': True, 'starKeyboardPreserved': True, 'pageErrors': errors}
    (args.output_dir / 'report.json').write_text(json.dumps(report, indent=2), 'utf-8')
    print(json.dumps(report, indent=2))
    context.close()
    browser.close()
