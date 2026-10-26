"""Check catalog restore across editor reloads and usability during incremental refresh."""
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
plugins = [dict(id=f'effect-{i}', classId=f'effect-{i}', name=f'Effect {i:03}', vendor=f'Vendor {i % 20:02}',
                path=f'C:/VST3/Effect{i}.vst3', version='1', format='vst3', isInstrument=False)
           for i in range(400)]
seed = {'plugins': plugins, 'failures': [], 'scanned': 0, 'cached': 400, 'hasCache': True}
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, args=['--no-proxy-server'])
    context = browser.new_context(viewport={'width': 1400, 'height': 900})
    page = context.new_page()
    page.set_default_timeout(15000)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.add_init_script(f"""(() => {{
      localStorage.setItem('pymss-plugin-library-directories-v1', JSON.stringify(['C:/VST3']));
      if(!localStorage.getItem('mock-plugin-catalog'))localStorage.setItem('mock-plugin-catalog',JSON.stringify({json.dumps(seed)}));
    }})()""")
    page.add_init_script(f"({fixtures['MOCK']})(true)")

    def open_editor():
        page.goto(args.url.rstrip('/') + '/#/editor', wait_until='networkidle')
        page.evaluate(fixtures['SETUP'])
        toggle = page.locator('.editor-shell__inspector-toggle')
        if toggle.get_attribute('aria-pressed') == 'false':
            toggle.click()
        page.locator('.editor-inspector .plugin-panel').get_by_role('button', name='音频插件', exact=True).click()
        return page.get_by_role('dialog', name='插件管理', exact=True)

    panel = open_editor()
    expect(panel.locator('.plugin-manager__library-count')).to_have_text('400')
    expect(panel.locator('.plugin-manager__group-heading')).to_have_count(20)
    assert page.evaluate("__vstCalls.filter(call=>call.command==='plugin_host_scan_report').length") == 0
    search = panel.locator('input[placeholder="搜索名称、厂商、类型或格式"]')
    search.fill('Effect 014')
    expect(panel.locator('.plugin-manager__plugin')).to_have_count(1)
    page.screenshot(path=str(args.output_dir / 'restored-400.png'))

    page.reload(wait_until='networkidle')
    panel = open_editor()
    expect(panel.locator('.plugin-manager__library-count')).to_have_text('400')
    assert page.evaluate("__vstCalls.filter(call=>call.command==='plugin_host_scan_report').length") == 0
    page.evaluate("""() => {
      __catalogOverride=JSON.parse(localStorage.getItem('mock-plugin-catalog')).plugins.filter(plugin=>plugin.id!=='effect-3');
      __catalogOverride.push({id:'new',classId:'new',name:'New Effect',vendor:'New Vendor',path:'C:/VST3/New.vst3',format:'vst3',isInstrument:false});
      __scanDelay=1500;
    }""")
    panel.get_by_role('button', name='识别插件', exact=True).click()
    expect(panel.locator('.plugin-manager__catalog')).to_have_attribute('aria-busy', 'true')
    search = panel.locator('input[placeholder="搜索名称、厂商、类型或格式"]')
    expect(search).to_be_enabled()
    search.fill('Effect 014')
    row = panel.locator('.plugin-manager__plugin')
    expect(row).to_be_enabled()
    row.dblclick()
    page.wait_for_function("__editor.session.tracks[0].pluginChain?.some(slot=>slot.pluginId==='effect-14')")
    page.wait_for_function("__vstCalls.some(call=>call.command==='plugin_host_scan_report')")
    expect(panel.locator('.plugin-manager__catalog')).to_have_attribute('aria-busy', 'false')
    assert page.evaluate("__vstCalls.filter(call=>call.command==='plugin_host_scan_report').at(-1).args.force") is False
    search.fill('Effect 003')
    expect(panel.locator('.plugin-manager__plugin')).to_have_count(0)
    search.fill('New Effect')
    expect(panel.locator('.plugin-manager__plugin')).to_have_count(1)
    page.screenshot(path=str(args.output_dir / 'incremental-refresh.png'))
    page.evaluate("__editor.session={...__editor.session,id:'next-project',pluginChain:[],tracks:[],sources:[]}")
    page.locator('.editor-inspector .plugin-panel').get_by_role('button', name='音频插件', exact=True).click()
    expect(panel.locator('.plugin-manager__library-count')).to_have_text('400')
    assert not errors, errors
    report = {'cacheRestored': 400, 'reloadRequiresScan': False, 'refreshUsesForce': False,
              'addWhileRefreshing': True, 'removedAndNewPluginsUpdated': True,
              'projectSwitchPreservesCatalog': True, 'pageErrors': errors}
    (args.output_dir / 'report.json').write_text(json.dumps(report, indent=2), 'utf-8')
    print(json.dumps(report, indent=2))
    context.close()
    browser.close()
