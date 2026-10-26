"""Verify floating plugin menus using physical mouse input and mocked Tauri IPC."""
import argparse
import ast
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--url', default='http://127.0.0.1:1439')
parser.add_argument('--output-dir', type=Path, required=True)
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)
module = ast.parse(Path(__file__).with_name('editor_vst_ui_smoke.py').read_text('utf-8'))
fixtures = {node.targets[0].id: ast.literal_eval(node.value) for node in module.body
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in ('MOCK', 'SETUP')}
locales = {locale: json.loads((Path(__file__).parents[2] / 'src' / 'i18n' / f'{locale}.json').read_text('utf-8'))['editor']['plugins']
           for locale in ('zh-CN', 'en')}


def settle(panel):
    panel.evaluate('''async el => {
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      await Promise.all(el.getAnimations({subtree:true}).map(animation=>animation.finished.catch(()=>{})));
    }''')


def click_center(page, locator):
    # Do not let locator.click() scroll an overflow:hidden ancestor and hide clipping bugs.
    box = locator.bounding_box()
    assert box is not None
    hit = locator.evaluate('''el => {
      const r=el.getBoundingClientRect();
      return el.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));
    }''')
    assert hit, 'Control is covered or clipped at its visible center'
    page.mouse.click(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)


def exercise_layout_menus(page, panel, labels, theme):
    page.evaluate("""() => {__editor.session.pluginChain=[{
      id:'gain',pluginId:'gain',name:'Precision Gain',path:'C:/VST3/Gain.vst3',format:'vst3',bypassed:false}]}""")
    panel.locator('.plugin-panel__slot').get_by_role('button', name=labels['parameters'], exact=True).click()
    expect(panel.locator('.plugin-parameters__row')).to_have_count(1)
    source_button = panel.get_by_role('button', name=labels['manageSources'], exact=True)
    popup = panel.locator('.plugin-manager__source-manager')
    handles = {
        'window': panel.locator('.plugin-manager__corner--ne'),
        'library': panel.locator('.plugin-manager__resize-separator--library'),
        'parameters': panel.locator('.plugin-manager__resize-separator--parameters'),
    }
    results = []
    for kind, method in (
        ('window', 'pointer'), ('library', 'pointer'), ('parameters', 'pointer'),
        ('window', 'keyboard'), ('library', 'keyboard'), ('parameters', 'keyboard'),
        ('window', 'reset'), ('library', 'reset'), ('parameters', 'reset'), ('viewport', 'resize'),
    ):
        panel.locator('.plugin-manager__corner--se').dblclick()
        settle(panel)
        source_button.click()
        expect(popup).to_be_visible()
        settle(panel)
        if method == 'pointer':
            box = handles[kind].bounding_box()
            x = box['x'] + box['width'] / 2
            y = box['y'] + (5 if kind == 'library' else box['height'] / 2)
            page.mouse.move(x, y)
            page.mouse.down()
            expect(source_button).to_have_attribute('aria-expanded', 'false')
            page.mouse.move(x + (80 if kind == 'library' else 0),
                            y + (1000 if kind == 'window' else -40 if kind == 'parameters' else 0), steps=8)
            page.mouse.up()
        elif method == 'keyboard':
            handles[kind].press('ArrowRight' if kind == 'library' else 'ArrowUp')
        elif method == 'reset':
            box = handles[kind].bounding_box()
            handles[kind].dblclick(position={'x': box['width'] / 2,
                                            'y': 5 if kind == 'library' else box['height'] / 2})
        else:
            page.set_viewport_size({'width': 980, 'height': 680})
        expect(popup).not_to_be_visible()
        expect(source_button).to_have_attribute('aria-expanded', 'false')
        expect(panel).to_be_visible()
        if method == 'resize':
            page.set_viewport_size({'width': 1400, 'height': 900})
        settle(panel)
        assert page.evaluate("JSON.parse(localStorage.getItem('pymss-plugin-library-directories-v1')).length") == 9
        assert page.evaluate("__vstCalls.filter(call=>call.args?.request?.op==='play').length") == 0
        results.append({'theme': theme, 'scenario': f'dismiss-menu-before-{kind}-{method}'})
    return results


results = []
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, args=['--no-proxy-server'])
    for theme in ('light', 'dark'):
        for locale, labels in locales.items():
            for width, height in ((1400, 900), (980, 680), (800, 600)):
                context = browser.new_context(viewport={'width': width, 'height': height})
                page = context.new_page()
                page.set_default_timeout(15000)
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                mock = fixtures['MOCK'].replace("themeMode:'light'", 'themeMode:' + repr(theme))
                mock = mock.replace("locale:'zh-CN'", 'locale:' + repr(locale))
                page.add_init_script(f'({mock})(true)')
                page.add_init_script("""localStorage.setItem('pymss-plugin-library-directories-v1',
                  JSON.stringify(Array.from({length:10},(_,i)=>'C:/Audio/VST'+i)));
                  localStorage.setItem('pymss-plugin-manager-window-v1',JSON.stringify({width:680,height:480}));""")
                try:
                    for position in ('center', 'bottom-right'):
                        page.goto(args.url.rstrip('/') + f'/?popover_case={position}#/editor', wait_until='networkidle')
                        page.evaluate(fixtures['SETUP'])
                        page.evaluate("""() => {
                          const first=__editor.session.tracks[0];
                          __editor.session.tracks.push(...Array.from({length:11},(_,i)=>({...first,id:'track-'+i,name:'Track '+i,clips:[]})));
                        }""")
                        toggle = page.locator('.editor-shell__inspector-toggle')
                        if toggle.get_attribute('aria-pressed') == 'false':
                            toggle.click()
                        page.locator('.plugin-panel').get_by_role('button', name=labels['title'], exact=True).click()
                        panel = page.get_by_role('dialog', name=labels['managerTitle'], exact=True)
                        expect(panel).to_be_visible()
                        settle(panel)
                        if position == 'bottom-right':
                            box = panel.locator('.plugin-manager__heading > strong').bounding_box()
                            x, y = box['x'] + box['width'] / 2, box['y'] + box['height'] / 2
                            page.mouse.move(x, y)
                            page.mouse.down()
                            page.mouse.move(x + width, y + height, steps=10)
                            page.mouse.up()
                            settle(panel)
                        initial = panel.bounding_box()
                        panel.get_by_role('button', name=labels['manageSources'], exact=True).click()
                        popup = panel.locator('.plugin-manager__source-manager')
                        expect(popup).to_be_visible()
                        settle(panel)
                        bounds = popup.bounding_box()
                        assert bounds['x'] >= 0 and bounds['y'] >= 0, bounds
                        assert bounds['x'] + bounds['width'] <= width + 1, bounds
                        assert bounds['y'] + bounds['height'] <= height + 1, bounds
                        content = popup.locator('.plugin-manager__source-content')
                        scroll = content.bounding_box()
                        page.mouse.move(scroll['x'] + scroll['width'] / 2, scroll['y'] + scroll['height'] / 2)
                        page.mouse.wheel(0, 1200)
                        page.wait_for_function('''() => {
                          const el=document.querySelector('.plugin-manager__source-content');
                          return el.scrollTop>=el.scrollHeight-el.clientHeight-1;
                        }''')
                        click_center(page, popup.locator('.plugin-panel__directory').last.get_by_role('button'))
                        page.wait_for_function("JSON.parse(localStorage.getItem('pymss-plugin-library-directories-v1')).length===9")
                        assert panel.evaluate('(el)=>el.scrollTop') == 0
                        assert panel.bounding_box() == initial
                        page.screenshot(path=str(args.output_dir / f'{theme}-{locale}-{width}-{position}.png'))
                        panel.focus()
                        page.keyboard.press('Escape')
                        expect(popup).not_to_be_visible()
                        expect(panel).to_be_visible()
                        panel.locator('.plugin-manager__target .n-select').click()
                        option = panel.locator('.n-base-select-option').filter(has_text=labels['masterOutput'])
                        expect(option).to_be_visible()
                        settle(panel)
                        click_center(page, option)
                        page.wait_for_function('__editor.selectedTrackId===null')
                        assert panel.bounding_box() == initial
                        assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
                        if locale == 'zh-CN' and width == 1400 and position == 'center':
                            results.extend(exercise_layout_menus(page, panel, labels, theme))
                        panel.press('Escape')
                        expect(panel).not_to_be_visible()
                        results.append({'theme': theme, 'locale': locale, 'viewport': [width, height],
                                        'position': position, 'popup': bounds, 'removedLastDirectory': True})
                    assert not errors, errors
                except Exception:
                    page.screenshot(path=str(args.output_dir / f'{theme}-{locale}-{width}-failure.png'))
                    raise
                finally:
                    context.close()
    browser.close()
(args.output_dir / 'report.json').write_text(json.dumps({'results': results,
    'limitations': 'Real Vue UI with mock IPC; native plugins and DSP are not exercised.'}, indent=2), encoding='utf-8')
print(json.dumps({'passed': len(results)}))
