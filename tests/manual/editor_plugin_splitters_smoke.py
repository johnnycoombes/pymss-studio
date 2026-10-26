"""Verify plugin workspace splitters with real pointer input and mocked Tauri IPC."""
import argparse
import ast
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from plugin_catalog_helpers import expand_catalog_groups

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--url', default='http://127.0.0.1:1439')
parser.add_argument('--output-dir', type=Path, required=True)
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)
module = ast.parse(Path(__file__).with_name('editor_vst_ui_smoke.py').read_text('utf-8'))
fixtures = {node.targets[0].id: ast.literal_eval(node.value) for node in module.body
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in ('MOCK', 'SETUP')}
storage_key = 'pymss-plugin-manager-layout-v1'
mock = fixtures['MOCK'].replace('const request=args.request;', """const request=args.request;
  if(request.op==='parameters')return {projectId:request.projectId,sessionId:__vstState.sessionId,
    params:Array.from({length:36},(_,i)=>({id:String(i),name:`Control ${i}`,min:0,max:1,default:0,unit:'None',choices:[]})),
    values:Object.fromEntries(Array.from({length:36},(_,i)=>[String(i),0.5]))};
""")


def open_manager(page, setup=False):
    if setup:
        page.goto(args.url.rstrip('/') + '/#/editor', wait_until='networkidle')
        page.evaluate(fixtures['SETUP'])
        toggle = page.locator('.editor-shell__inspector-toggle')
        if toggle.get_attribute('aria-pressed') == 'false':
            toggle.click()
        page.evaluate("""() => {__editor.session.tracks[0].pluginChain=[{
          id:'gain',pluginId:'gain',name:'Precision Gain',path:'C:/VST3/Gain.vst3',format:'vst3',bypassed:false}]}""")
    page.locator('.plugin-panel').get_by_role('button', name='音频插件', exact=True).click()
    panel = page.get_by_role('dialog', name='插件管理', exact=True)
    expect(panel).to_be_visible()
    if panel.locator('.plugin-manager__parameter-dock').count() == 0:
        panel.locator('.plugin-panel__slot').first.get_by_role('button', name='参数', exact=True).click()
    expect(panel.locator('.plugin-parameters__row')).to_have_count(12)
    return panel


def dimensions(panel):
    return panel.evaluate("""el => {
      const rect=node=>{const r=node.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height}};
      const library=el.querySelector('.plugin-manager__library'),dock=el.querySelector('.plugin-manager__parameter-dock');
      const outer=[el,...el.querySelectorAll('.plugin-manager__body,.plugin-manager__library,.plugin-manager__chain,.plugin-manager__parameter-dock')];
      return {panel:rect(el),library:rect(library),dock:rect(dock),slots:rect(el.querySelector('.plugin-manager__slots')),
        overflow:outer.filter(node=>node.scrollWidth>node.clientWidth+1||node.scrollHeight>node.clientHeight+1).map(node=>node.classList[0])};
    }""")


def drag(page, handle, dx=0, dy=0, release=True):
    box = handle.bounding_box()
    x, y = box['x'] + box['width'] / 2, box['y'] + box['height'] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + dx, y + dy, steps=8)
    if release:
        page.mouse.up()


def stable(page):
    page.evaluate('''async () => {
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      await Promise.all(document.querySelector('.plugin-manager').getAnimations({subtree:true}).map(animation=>animation.finished.catch(()=>{})));
    }''')


def assert_parameter_controls(page, panel):
    chain = panel.locator('.plugin-manager__chain')
    bounds = chain.bounding_box()
    page.mouse.move(bounds['x'] + bounds['width'] - 5, bounds['y'] + bounds['height'] / 2)
    page.mouse.wheel(0, 1200)
    page.wait_for_function('''() => {
      const el=document.querySelector('.plugin-manager__chain');
      return el.scrollTop >= el.scrollHeight-el.clientHeight-1;
    }''')
    stable(page)
    dock = panel.locator('.plugin-manager__parameter-dock')
    geometry = dock.evaluate('''el => {
      const bounds=el.getBoundingClientRect(),chain=el.parentElement.getBoundingClientRect();
      const list=el.querySelector('.plugin-parameters__list');
      const fixed=[el.querySelector('.plugin-parameters__toolbar'),el.querySelector('.n-pagination')].filter(Boolean);
      return {listHeight:list.clientHeight,dockHeight:el.clientHeight,
        clipped:fixed.some(node=>{const r=node.getBoundingClientRect();return r.top<Math.max(bounds.top,chain.top)-1||r.bottom>Math.min(bounds.bottom,chain.bottom)+1})};
    }''')
    assert geometry['listHeight'] >= 28 and not geometry['clipped'], geometry
    pagination = dock.locator('.n-pagination')
    pagination.get_by_text('2', exact=True).click()
    expect(dock.get_by_role('textbox', name='Control 12', exact=True)).to_be_visible()
    expect(dock.get_by_role('textbox', name='Control 12', exact=True)).to_be_enabled()
    pagination.get_by_text('1', exact=True).click()
    page.mouse.move(bounds['x'] + bounds['width'] - 5, bounds['y'] + bounds['height'] / 2)
    page.mouse.wheel(0, -1200)
    page.wait_for_function('document.querySelector(".plugin-manager__chain").scrollTop < 1')
    stable(page)
    expect(panel.locator('.plugin-panel__slot').first.get_by_role('button', name='打开界面', exact=True)).to_be_in_viewport()


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
            panel = open_manager(page, setup=True)
            library = panel.get_by_role('separator', name='调整插件库宽度')
            parameters = panel.get_by_role('separator', name='调整参数区高度')
            expect(library).to_have_attribute('aria-orientation', 'vertical')
            expect(parameters).to_have_attribute('aria-orientation', 'horizontal')
            stable(page)
            initial = dimensions(panel)
            drag(page, library, dx=150)
            drag(page, parameters, dy=-90)
            stable(page)
            resized = dimensions(panel)
            assert abs(resized['library']['width'] - initial['library']['width'] - 150) < 1, resized
            assert abs(resized['dock']['height'] - initial['dock']['height'] - 90) < 1, resized
            assert resized['panel'] == initial['panel'], 'Dragging a splitter must not move the dialog'
            assert not resized['overflow'], resized
            saved = page.evaluate('key=>JSON.parse(localStorage.getItem(key))', storage_key)
            panel.press('Escape')
            expect(panel).not_to_be_visible()
            panel = open_manager(page)
            stable(page)
            assert abs(dimensions(panel)['library']['width'] - saved['libraryWidth']) < 1, (dimensions(panel), saved)
            assert abs(dimensions(panel)['dock']['height'] - saved['parameterHeight']) < 1
            results.append({'theme': theme, 'scenario': 'independent-drag-and-reopen', 'geometry': resized})

            for width, height in ((980, 680), (800, 600), (1400, 900)):
                page.set_viewport_size({'width': width, 'height': height})
                stable(page)
                current = dimensions(panel)
                assert not current['overflow'], current
                assert current['slots']['height'] >= 95, current
                assert current['panel']['x'] >= 0 and current['panel']['y'] >= 0, current
                assert current['panel']['x'] + current['panel']['width'] <= width + 1, current
                assert current['panel']['y'] + current['panel']['height'] <= height + 1, current
                assert page.evaluate('key=>JSON.parse(localStorage.getItem(key))', storage_key) == saved
                page.screenshot(path=str(args.output_dir / f'{theme}-{width}x{height}.png'))
                results.append({'theme': theme, 'scenario': f'responsive-{width}x{height}', 'geometry': current})
            assert abs(dimensions(panel)['dock']['height'] - saved['parameterHeight']) < 1
            assert abs(dimensions(panel)['library']['width'] - saved['libraryWidth']) < 1

            for handle, kind, delta in ((library, 'library', {'dx': 2500}), (parameters, 'parameters', {'dy': -2500})):
                drag(page, handle, **delta)
                assert abs(float(handle.get_attribute('aria-valuenow')) - float(handle.get_attribute('aria-valuemax'))) < 1
                assert not dimensions(panel)['overflow']
                handle.press('Home')
                assert abs(float(handle.get_attribute('aria-valuenow')) - float(handle.get_attribute('aria-valuemin'))) < 1
                assert not dimensions(panel)['overflow']
                before = float(handle.get_attribute('aria-valuenow'))
                handle.press('ArrowRight' if kind == 'library' else 'ArrowUp')
                assert abs(float(handle.get_attribute('aria-valuenow')) - before - 16) < 1
                handle.dblclick()
            stable(page)
            assert page.evaluate('key=>JSON.parse(localStorage.getItem(key))', storage_key) == {}
            assert abs(dimensions(panel)['library']['width'] - initial['library']['width']) < 1
            assert abs(dimensions(panel)['dock']['height'] - initial['dock']['height']) < 1

            drag(page, library, dx=60, release=False)
            page.keyboard.press('Escape')
            page.mouse.up()
            expect(panel).to_be_visible()
            assert abs(dimensions(panel)['library']['width'] - initial['library']['width']) < 1
            assert 'plugin-manager--resizing-library' not in panel.get_attribute('class')
            library.press('ArrowRight')
            parameters.press('ArrowUp')
            saved = page.evaluate('key=>JSON.parse(localStorage.getItem(key))', storage_key)
            results.append({'theme': theme, 'scenario': 'bounds-keyboard-reset-and-escape'})

            panel = open_manager(page, setup=True)
            stable(page)
            assert abs(dimensions(panel)['library']['width'] - saved['libraryWidth']) < 1
            assert abs(dimensions(panel)['dock']['height'] - saved['parameterHeight']) < 1
            results.append({'theme': theme, 'scenario': 'reload-preserves-preferences'})

            # Resize the outer floating window while its two internal splitters are in use.
            def corner_handle(corner):
                return panel.locator(f'.plugin-manager__corner--{corner}')

            drag(page, corner_handle('se'), dx=-400, dy=-240)
            stable(page)
            assert not dimensions(panel)['overflow'], dimensions(panel)
            drag(page, panel.locator('.plugin-manager__heading > strong'), dx=80, dy=60)
            stable(page)
            for corner in ('nw', 'ne', 'sw', 'se'):
                before = dimensions(panel)['panel']
                drag(page, corner_handle(corner), dx=-35 if corner.endswith('w') else 35,
                     dy=-24 if corner.startswith('n') else 24)
                stable(page)
                after = dimensions(panel)['panel']
                assert abs(after['width'] - before['width'] - 35) < 1, (corner, before, after)
                assert abs(after['height'] - before['height'] - 24) < 1, (corner, before, after)
                for axis, size, opposite in (('x', 'width', corner.endswith('w')), ('y', 'height', corner.startswith('n'))):
                    old_anchor = before[axis] + (before[size] if opposite else 0)
                    new_anchor = after[axis] + (after[size] if opposite else 0)
                    assert abs(old_anchor - new_anchor) < 1, (corner, before, after)
                assert not dimensions(panel)['overflow'], dimensions(panel)
                results.append({'theme': theme, 'scenario': f'corner-{corner}-opposite-anchor'})

            before = dimensions(panel)['panel']
            drag(page, panel.locator('.plugin-manager__heading > strong'), dx=30, dy=20)
            stable(page)
            moved = dimensions(panel)['panel']
            assert abs(moved['x'] - before['x'] - 30) < 1, (before, moved)
            assert abs(moved['y'] - before['y'] - 20) < 1, (before, moved)
            drag(page, library, dx=20)
            stable(page)
            assert dimensions(panel)['panel'] == moved, 'Internal resizing must preserve the moved dialog'
            drag(page, corner_handle('nw'), dx=25, dy=20)
            stable(page)
            resized = dimensions(panel)['panel']
            assert abs(resized['x'] + resized['width'] - moved['x'] - moved['width']) < 1
            assert abs(resized['y'] + resized['height'] - moved['y'] - moved['height']) < 1
            drag(page, corner_handle('se'), dx=-40, dy=-30, release=False)
            page.keyboard.press('Escape')
            page.mouse.up()
            stable(page)
            expect(panel).to_be_visible()
            assert dimensions(panel)['panel'] == resized
            results.append({'theme': theme, 'scenario': 'header-drag-splitter-and-corner-cancel'})

            page.evaluate("__pickedDirectory='C:/Audio/VST3'")
            panel.get_by_role('button', name='选择插件目录', exact=True).click()
            expand_catalog_groups(panel)
            expect(panel.locator('.plugin-manager__plugin')).to_have_count(3)
            panel.locator('.plugin-manager__plugin').filter(has_text='Precision Gain').click()
            panel.locator('.plugin-manager__add-button').click()
            expect(panel.locator('.plugin-manager__feedback')).to_be_visible()
            drag(page, corner_handle('se'), dx=-2500, dy=-2500)
            stable(page)
            smallest = dimensions(panel)
            assert abs(smallest['panel']['width'] - 680) < 1, smallest
            assert abs(smallest['panel']['height'] - 480) < 1, smallest
            assert not smallest['overflow'], smallest
            page.screenshot(path=str(args.output_dir / f'{theme}-window-minimum.png'))
            page.evaluate('__editor.pluginStateError="The project could not be saved (os error 1175)"')
            expect(panel.locator('.n-alert')).to_be_visible()
            stable(page)
            assert set(dimensions(panel)['overflow']).issubset({'plugin-manager__chain'}), dimensions(panel)
            assert_parameter_controls(page, panel)
            expect(panel.get_by_role('button', name='重试保存', exact=True)).to_be_in_viewport()
            expect(panel.get_by_role('button', name='收起参数', exact=True)).to_be_in_viewport()
            page.screenshot(path=str(args.output_dir / f'{theme}-window-minimum-error.png'))
            page.evaluate('__editor.pluginStateError=null')
            results.append({'theme': theme, 'scenario': 'minimum-window-with-feedback-and-save-error'})
            corner_handle('se').press('ArrowRight')
            stable(page)
            assert abs(dimensions(panel)['panel']['width'] - 696) < 1
            panel.press('Escape')
            expect(panel).not_to_be_visible()
            panel = open_manager(page)
            stable(page)
            assert abs(dimensions(panel)['panel']['width'] - 696) < 1
            panel = open_manager(page, setup=True)
            stable(page)
            assert abs(dimensions(panel)['panel']['width'] - 696) < 1
            results.append({'theme': theme, 'scenario': 'window-minimum-keyboard-and-reload'})

            drag(page, corner_handle('nw'), dx=-2500, dy=-2500)
            drag(page, corner_handle('se'), dx=2500, dy=2500)
            stable(page)
            largest = dimensions(panel)['panel']
            assert abs(largest['x']) < 1 and abs(largest['y']) < 1, largest
            assert abs(largest['width'] - 1400) < 1 and abs(largest['height'] - 900) < 1, largest
            assert not dimensions(panel)['overflow'], dimensions(panel)
            page.set_viewport_size({'width': 980, 'height': 680})
            stable(page)
            narrowed = dimensions(panel)['panel']
            assert narrowed['x'] >= 0 and narrowed['y'] >= 0, narrowed
            assert narrowed['x'] + narrowed['width'] <= 981 and narrowed['y'] + narrowed['height'] <= 681, narrowed
            page.set_viewport_size({'width': 1400, 'height': 900})
            stable(page)
            assert abs(dimensions(panel)['panel']['width'] - 1400) < 1
            corner_handle('se').dblclick()
            stable(page)
            assert dimensions(panel)['panel'] == initial['panel']
            assert page.evaluate('localStorage.getItem("pymss-plugin-manager-window-v1")') is None
            results.append({'theme': theme, 'scenario': 'window-bounds-resolution-and-reset'})
            assert not errors, errors
        except Exception:
            page.screenshot(path=str(args.output_dir / f'{theme}-failure.png'))
            raise
        finally:
            context.close()
    browser.close()
(args.output_dir / 'report.json').write_text(json.dumps({'results': results,
    'limitations': 'Real Vue UI with mock IPC; native plugin processing is outside this test.'}, indent=2), encoding='utf-8')
print(json.dumps({'passed': len(results)}))
