"""Verify canvas double-click and type-filtered connection search gestures."""

import argparse
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument('--url', default='http://127.0.0.1:1448')
parser.add_argument('--output-dir', type=Path, default=Path('.tmp/connection-search'))
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, args=['--no-proxy-server'])
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    page.set_default_timeout(10000)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.add_init_script('''localStorage.setItem('pymss-studio:app-settings', JSON.stringify({
      startupOnboardingSeen:true,locale:'zh-CN',outputDir:'D:/outputs'
    }))''')
    page.goto(args.url + '/#/workflow-node-editor?new=1')
    page.wait_for_load_state('networkidle')
    page.add_style_tag(content='.title-bar {display:none !important}')
    canvas = page.locator('canvas.lg-canvas')
    expect(canvas).to_be_visible()
    page.wait_for_function("document.querySelector('canvas.lg-canvas')?.data?.graph.nodes.length===4")
    canvas.evaluate('''el=>{
      const c=el.data;
      const positions={pymss_load_audio:[60,140],pymss_mss_params:[60,420],
        mss_separate:[360,280],pymss_save_audio:[850,280]};
      for(const node of c.graph.nodes) node.pos=positions[node.type];
      c.ds.scale=1;c.ds.offset=[0,0];c.draw(true,true);
    }''')

    def point(graph_position):
        offset = canvas.evaluate('(el,p)=>el.data.ds.convertOffsetToCanvas(p)', graph_position)
        box = canvas.bounding_box()
        return box['x'] + offset[0], box['y'] + offset[1]

    # An empty-area double-click should neither open search nor create a node.
    page.mouse.dblclick(*point([30,30]), delay=50)
    expect(page.locator('.litesearchbox')).to_have_count(0)
    assert canvas.evaluate('el=>el.data.graph.nodes.length') == 4

    # Preserve the native node double-click gesture.
    canvas.evaluate("el=>{window.__canvasGestures=[];el.addEventListener('litegraph:canvas',e=>window.__canvasGestures.push(e.detail.subType))}")
    page.mouse.dblclick(*point([140,130]), delay=50)
    page.wait_for_function("window.__canvasGestures.includes('node-double-click')")

    # Link-drop menus are disabled by the library by default. Invoke its public
    # menu API to verify that suppressing double-click does not disable Search.
    canvas.evaluate('''el=>{
      const c=el.data,node=c.graph.nodes.find(n=>n.type==='pymss_load_audio');
      const p=c.ds.convertOffsetToCanvas([650,90]),rect=el.getBoundingClientRect();
      c.showConnectionMenu({nodeFrom:node,slotFrom:node.outputs[0],
        e:new MouseEvent('pointerup',{clientX:rect.x+p[0],clientY:rect.y+p[1]}),
        showSearchBox:(event,options)=>c.showSearchBox(event,{...options,do_type_filter:true,
          show_general_after_typefiltered:false,show_general_if_none_on_typefilter:false})});
    }''')
    menu = page.locator('.litecontextmenu')
    expect(menu).to_be_visible()
    menu.locator('[data-value="Search"]').click()
    dialog = page.locator('.litesearchbox')
    expect(dialog).to_be_visible()
    assert dialog.locator('.slot_in_type_filter').input_value().upper() == 'AUDIO'
    expect(dialog.locator('[data-type="pymss_save_audio"]')).to_be_visible()
    search = dialog.locator('input.value')
    search.fill('PreviewAudio')
    search.press('End')
    expect(dialog.locator('.lite-search-item')).to_have_count(0)
    search.fill('pymss_save_audio')
    search.press('End')
    dialog.locator('[data-type="pymss_save_audio"]').click()
    expect(dialog).to_have_count(0)
    page.wait_for_function("document.querySelector('canvas.lg-canvas').data.graph.nodes.length===5")
    assert canvas.evaluate('''el=>{
      const graph=el.data.graph, node=graph.nodes.at(-1), link=graph.links[node.inputs[0].link];
      return node.type==='pymss_save_audio' && link?.origin_id===graph.nodes.find(n=>n.type==='pymss_load_audio').id;
    }''')

    page.screenshot(path=str(args.output_dir / 'connected-node.png'))
    assert not errors, errors
    browser.close()

print('PASS: only empty double-click is suppressed; native menu search retains AUDIO filtering and automatic connections')
