"""Check inline plugin toolbar bounds in the Vue inspector using mock Tauri IPC."""
import argparse
import ast
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--url', default='http://127.0.0.1:1436')
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
        for locale in ('zh-CN', 'en'):
            for panel_width in (240, 268):
                context = browser.new_context(viewport={'width': 1400, 'height': 900})
                page = context.new_page()
                page.set_default_timeout(15000)
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                mock = fixtures['MOCK'].replace("themeMode:'light'", 'themeMode:' + repr(theme))
                mock = mock.replace("locale:'zh-CN'", 'locale:' + repr(locale))
                mock = mock.replace("formats:['vst3']", "formats:['vst3','vst2']")
                page.add_init_script(f'({mock})(true)')
                page.add_init_script('localStorage.setItem("pymss-studio:editor-ui",' +
                                     json.dumps(json.dumps({'inspectorCollapsed': False,
                                                           'inspectorPanelWidth': panel_width,
                                                           'assetCollapsed': True})) + ')')
                try:
                    page.goto(args.url.rstrip('/') + '/#/editor', wait_until='domcontentloaded')
                    page.wait_for_load_state('networkidle')
                    page.locator('.editor-transport').wait_for(state='visible')
                    page.evaluate(fixtures['SETUP'])
                    page.evaluate("async () => {window.__host=await import('/src/utils/pluginHost.ts')}")
                    for viewport in ({'width': 1400, 'height': 900}, {'width': 980, 'height': 680}):
                        page.set_viewport_size(viewport)
                        for shell in (False, True):
                            for missing in (False, True):
                                page.evaluate("""async ({shell,missing}) => {
                                  const path='C:/VST/Effect'+(shell?'.dll':'.vst3');
                                  __missingPaths=missing?[path]:[];
                                  __editor.session.tracks[0].pluginChain=[{
                                    id:'effect',pluginId:shell?'vst2:shell':'gain',
                                    format:shell?'vst2':'vst3',isShellChild:shell,
                                    name:'Audio Effect',path,bypassed:missing,state:'sha256:initial'}];
                                  await __host.ensurePluginSession(__editor.session);
                                }""", {'shell': shell, 'missing': missing})
                                insert = page.locator('.plugin-panel .plugin-insert')
                                expect(insert).to_have_count(1)
                                expect(insert.locator('.plugin-manager__format')).to_have_text('VST2 Shell' if shell else 'VST3')
                                expect(insert.locator('.plugin-insert__missing')).to_have_count(int(missing))
                                geometry = insert.evaluate("""slot => {
                                  const pane=slot.closest('.inspector-strip__inserts');
                                  const boundary=pane.getBoundingClientRect();
                                  const tools=slot.querySelector('.plugin-insert__tools');
                                  const elements=[...tools.children,...tools.querySelectorAll('button')];
                                  return {width:pane.clientWidth,scrollWidth:pane.scrollWidth,
                                    clipped:elements.filter(element=>{
                                      const r=element.getBoundingClientRect();
                                      return r.left<boundary.left-.5||r.right>boundary.right+.5;
                                    }).map(element=>element.getAttribute('aria-label')||element.textContent.trim())};
                                }""")
                                assert geometry['scrollWidth'] <= geometry['width'] + 1, geometry
                                assert not geometry['clipped'], geometry
                                assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
                                results.append({'theme': theme, 'locale': locale, 'panelWidth': panel_width,
                                                'viewport': viewport, 'shell': shell, 'missing': missing,
                                                'geometry': geometry})
                                if panel_width == 240 and viewport['width'] == 980 and shell and missing:
                                    page.screenshot(path=str(args.output_dir / f'{theme}-{locale}-narrow-shell-missing.png'))
                    assert not errors, errors
                    # Ensure the fully visible delete button remains actionable after wrapping.
                    insert.locator('.plugin-insert__tools button').last.click()
                    page.wait_for_function('__editor.session.tracks[0].pluginChain.length === 0')
                except Exception:
                    page.screenshot(path=str(args.output_dir / f'{theme}-{locale}-{panel_width}-failure.png'))
                    raise
                finally:
                    context.close()
    browser.close()
(args.output_dir / 'report.json').write_text(json.dumps({'results': results,
    'limitations': 'Real Vue layout with mock IPC; native plugins and DSP are not exercised.'}, indent=2), encoding='utf-8')
print(json.dumps({'passed': len(results)}))
