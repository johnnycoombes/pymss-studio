"""Check editor panel preferences and responsive plugin layouts with mock IPC."""
import argparse
import ast
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--url', default='http://127.0.0.1:1437')
parser.add_argument('--output-dir', type=Path, required=True)
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)
module = ast.parse(Path(__file__).with_name('editor_vst_ui_smoke.py').read_text('utf-8'))
fixtures = {node.targets[0].id: ast.literal_eval(node.value) for node in module.body
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in ('MOCK', 'SETUP')}
sizes = [(3840, 2160), (2560, 1440), (1920, 1080), (1536, 864), (1366, 768), (1280, 720), (980, 680), (1024, 576), (800, 600)]
results = []


def bounds(page, manager):
    result = page.evaluate("""() => {
      const shell=document.querySelector('.editor-shell').getBoundingClientRect();
      const center=document.querySelector('.editor-shell__center').getBoundingClientRect();
      const dialog=document.querySelector('.plugin-manager').getBoundingClientRect();
      const clipped=[...document.querySelectorAll('.plugin-manager__parameter-heading,.plugin-parameters__toolbar,.plugin-manager__library-footer,.plugin-manager__target')].filter(el=>{
        const r=el.getBoundingClientRect();return r.width&&r.height&&(r.left<dialog.left-.5||r.right>dialog.right+.5||r.top<dialog.top-.5||r.bottom>dialog.bottom+.5);
      }).map(el=>el.className);
      return {shell:{width:shell.width,height:shell.height,bottom:shell.bottom},centerWidth:center.width,
        dialog:{left:dialog.left,top:dialog.top,right:dialog.right,bottom:dialog.bottom},clipped,
        pageOverflow:document.documentElement.scrollWidth>innerWidth};
    }""")
    assert not result['pageOverflow'] and not result['clipped'], result
    assert result['centerWidth'] >= (519 if page.viewport_size['width'] >= 980 else 359), result
    assert result['shell']['bottom'] <= page.viewport_size['height'] + 1, result
    dialog = result['dialog']
    assert dialog['left'] >= 0 and dialog['top'] >= 0, result
    assert dialog['right'] <= page.viewport_size['width'] + 1, result
    assert dialog['bottom'] <= page.viewport_size['height'] + 1, result
    expect(manager.locator('.plugin-parameters__toolbar button')).to_be_in_viewport()
    return result


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, args=['--no-proxy-server'])
    for theme in ('light', 'dark'):
        for locale in ('zh-CN', 'en'):
            context = browser.new_context(viewport={'width': 1920, 'height': 1080})
            page = context.new_page()
            page.set_default_timeout(15000)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            mock = fixtures['MOCK'].replace("themeMode:'light'", 'themeMode:' + repr(theme))
            mock = mock.replace("locale:'zh-CN'", 'locale:' + repr(locale))
            page.add_init_script(f'({mock})(true)')
            preferences = {'assetCollapsed': False, 'inspectorCollapsed': False,
                           'assetPanelWidth': 300, 'inspectorPanelWidth': 320, 'panelPriority': 'inspector'}
            page.add_init_script('localStorage.setItem("pymss-studio:editor-ui",' + json.dumps(json.dumps(preferences)) + ')')
            try:
                page.goto(args.url.rstrip('/') + '/#/editor', wait_until='domcontentloaded')
                page.wait_for_load_state('networkidle')
                page.locator('.editor-transport').wait_for(state='visible')
                page.evaluate(fixtures['SETUP'])
                page.evaluate("""() => {
                  __editor.session.name='Long session name · 多音轨混音与插件处理';
                  __editor.session.tracks[0].pluginChain=Array.from({length:12},(_,index)=>({
                    id:'effect-'+index,format:'vst3',pluginId:'gain',name:'Audio Effect '+index,
                    path:'C:/VST3/Gain.vst3',bypassed:false,state:'sha256:initial'}));
                }""")
                inspector = page.locator('.editor-inspector')
                expect(inspector).to_be_visible()
                page.locator('.plugin-panel__header button').click()
                manager = page.get_by_role('dialog', name='插件管理' if locale == 'zh-CN' else 'Plugin manager', exact=True)
                expect(manager).to_be_visible()
                manager.locator('.plugin-panel__slot').first.get_by_role('button', name='参数' if locale == 'zh-CN' else 'Parameters', exact=True).click()
                expect(manager.locator('.plugin-parameters__toolbar button')).to_be_visible()
                for width, height in sizes:
                    page.set_viewport_size({'width': width, 'height': height})
                    page.wait_for_timeout(250)
                    geometry = bounds(page, manager)
                    stored = page.evaluate('JSON.parse(localStorage.getItem("pymss-studio:editor-ui"))')
                    assert stored == preferences, stored
                    if width <= 980:
                        assert page.locator('.editor-shell__asset-panel').evaluate("""panel => {
                          panel.querySelector('input,button')?.focus();
                          return panel.inert && !panel.contains(document.activeElement);
                        }""")
                    results.append({'theme': theme, 'locale': locale, 'viewport': [width, height], 'geometry': geometry})
                    page.screenshot(path=str(args.output_dir / f'{theme}-{locale}-{width}x{height}.png'))
                    if width in (2560, 980):
                        manager.press('Escape')
                        expect(manager).not_to_be_visible()
                        page.screenshot(path=str(args.output_dir / f'{theme}-{locale}-workspace-{width}x{height}.png'))
                        page.locator('.plugin-panel__header button').click()
                        expect(manager).to_be_visible()
                manager.press('Escape')
                expect(manager).not_to_be_visible()
                page.set_viewport_size({'width': 1920, 'height': 1080})
                page.wait_for_timeout(250)
                asset = page.locator('.editor-shell__assets')
                assert asset.bounding_box()['width'] >= 333
                assert inspector.bounding_box()['width'] >= 319
                # Manual resizing updates the preferred width; later automatic fitting must retain it.
                divider = page.locator('.editor-shell__resizer--right').bounding_box()
                page.mouse.move(divider['x'] + divider['width'] / 2, divider['y'] + 100)
                page.mouse.down()
                page.mouse.move(divider['x'] + divider['width'] / 2 - 12, divider['y'] + 100)
                page.mouse.up()
                page.wait_for_function('JSON.parse(localStorage.getItem("pymss-studio:editor-ui")).inspectorPanelWidth===332')
                page.set_viewport_size({'width': 980, 'height': 680})
                page.wait_for_timeout(250)
                page.set_viewport_size({'width': 1920, 'height': 1080})
                page.wait_for_timeout(250)
                assert inspector.bounding_box()['width'] >= 331
                page.set_viewport_size({'width': 1024, 'height': 680})
                page.wait_for_function("""() => {
                  const shell=document.querySelector('.editor-shell');
                  return parseFloat(shell.style.getPropertyValue('--inspector-width'))===270
                    && Math.abs(document.querySelector('.editor-shell__inspector').getBoundingClientRect().width-270)<.5;
                }""")
                before_width = inspector.bounding_box()['width']
                divider = page.locator('.editor-shell__resizer--right').bounding_box()
                drag_x = divider['x'] + divider['width'] / 2
                page.mouse.move(drag_x, divider['y'] + 100)
                page.mouse.down()
                page.mouse.move(drag_x + 10, divider['y'] + 100)
                page.wait_for_function('(width)=>Math.abs(document.querySelector(".editor-inspector").getBoundingClientRect().width-width)<.5', arg=before_width - 10)
                page.mouse.move(drag_x, divider['y'] + 100)
                page.wait_for_function('(width)=>Math.abs(document.querySelector(".editor-inspector").getBoundingClientRect().width-width)<.5', arg=before_width)
                page.mouse.up()
                page.set_viewport_size({'width': 1920, 'height': 1080})
                page.wait_for_timeout(250)
                page.locator('.editor-shell__asset-toggle').click()
                page.wait_for_function('JSON.parse(localStorage.getItem("pymss-studio:editor-ui")).assetCollapsed===true')
                page.set_viewport_size({'width': 980, 'height': 680})
                page.set_viewport_size({'width': 1920, 'height': 1080})
                page.wait_for_timeout(250)
                assert asset.bounding_box()['width'] < 40
                assert not errors, errors
                results.append({'theme': theme, 'locale': locale, 'preferencesRestored': True, 'manualResizePreserved': True, 'reverseDragPreserved': True, 'pageErrors': errors})
            except Exception:
                page.screenshot(path=str(args.output_dir / f'{theme}-{locale}-failure.png'))
                (args.output_dir / f'{theme}-{locale}-failure.html').write_text(page.content(), encoding='utf-8')
                raise
            finally:
                context.close()
    browser.close()
(args.output_dir / 'report.json').write_text(json.dumps({'results': results,
    'limitations': 'Real Vue UI with mock IPC; no native plugin DSP or user data is used.'}, indent=2), encoding='utf-8')
print(json.dumps({'passed': len(results)}))
