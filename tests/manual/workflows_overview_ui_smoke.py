"""Headless checks for workflow previews, settings and persisted defaults."""

import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


simple_definition = {
    "version": 1,
    "defaults": {"device": "auto", "output_format": "wav"},
    "steps": [
        {
            "id": "step1",
            "model": "simple-model.ckpt",
            "input": "input",
            "stems": ["Vocals", "Instrumental"],
            "save": {"Vocals": "Default", "Instrumental": "Default"},
        }
    ],
}
advanced_definition = {
    "last_node_id": 4,
    "last_link_id": 3,
    "version": 0.4,
    "nodes": [
        {"id": 1, "type": "input_audio", "inputs": [], "outputs": [], "widgets_values": []},
        {
            "id": 2,
            "type": "mss_separate",
            "inputs": [],
            "outputs": [],
            "widgets_values": ["advanced-model.ckpt", "auto", True, "modelscope", "0", False],
        },
        {"id": 3, "type": "pymss_audio_normalize", "inputs": [], "outputs": [], "widgets_values": []},
        {"id": 4, "type": "pymss_save_audio", "inputs": [], "outputs": [], "widgets_values": ["wav", "44100", "FLOAT", "PCM_24", "320k"]},
    ],
    "links": [[1, 1, 0, 2, 0, "AUDIO"], [2, 2, 0, 3, 0, "AUDIO"], [3, 3, 0, 4, 0, "AUDIO"]],
}
branching_definition = {
    'version': 1,
    'steps': [
        {'id': 'first', 'model': 'voice-a.ckpt', 'input': 'input', 'stems': ['Vocals', 'Instrumental'], 'save': {}},
        {'id': 'second', 'model': 'voice-b.ckpt', 'input': 'input', 'stems': ['Vocals', 'Other'], 'save': {}},
    ],
    'ensembles': [{'id': 'blend', 'algorithm': 'avg_wave', 'output_stem': 'Vocals', 'save': 'Default',
                   'inputs': [{'source': 'first.Vocals', 'weight': 1}, {'source': 'second.Vocals', 'weight': 1}]}],
    'studio': {'editor': 'simple', 'viewport': {'x': 0, 'y': 0, 'zoom': 1}, 'nodes': {
        'input': {'x': 64, 'y': 220}, 'first': {'x': 500, 'y': 150}, 'second': {'x': 500, 'y': 480},
        'blend': {'x': 1000, 'y': 200}, 'save': {'x': 1420, 'y': 220},
    }},
}
workflow_state = {
    "selectedWorkflowId": "advanced",
    "workflows": [
        {
            "id": "advanced",
            "name": "Advanced routing",
            "description": "A graph workflow with processing nodes.",
            "definition": advanced_definition,
            "format": "graph",
            "formatVersion": 1,
            "createdAt": 2,
            "updatedAt": 2,
        },
        {
            "id": "simple",
            "name": "Simple vocal split",
            "description": "A compact two-stem workflow.",
            "definition": simple_definition,
            "format": "simple",
            "formatVersion": 1,
            "createdAt": 1,
            "updatedAt": 1,
        },
    ],
}


parser = argparse.ArgumentParser()
parser.add_argument('--url', default='http://127.0.0.1:1461')
args = parser.parse_args()
output = Path('data/outputs')
output.mkdir(parents=True, exist_ok=True)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, args=['--no-proxy-server'])
    page = browser.new_page(viewport={"width": 1280, "height": 850})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.add_init_script(
        f"""(() => {{
          localStorage.setItem(
            'pymss-studio:app-settings',
            JSON.stringify({{ startupOnboardingSeen: true, locale: 'en', themeAccent: 'teal' }}),
          )
          localStorage.setItem('pymss-studio:workflow-state', JSON.stringify({json.dumps(workflow_state)}))
        }})()"""
    )
    page.goto(args.url + '/#/workflows')
    page.wait_for_load_state("networkidle")

    expect(page.locator(".wf-list-group__head", has_text="Simple workflows")).to_be_visible()
    expect(page.locator(".wf-list-group__head", has_text="Advanced workflows")).to_be_visible()
    expect(page.locator(".wf-row--simple")).to_contain_text("Simple")
    expect(page.locator(".wf-row--simple")).to_contain_text("Steps 1 · Processing 0 · Outputs 2")
    expect(page.locator(".wf-row--advanced")).to_contain_text("Advanced")
    expect(page.locator(".wf-row--advanced")).to_contain_text("Nodes 4 · Outputs 1")

    page.locator(".wf-row--advanced").click()
    expect(page.locator(".wf-kind-badge--advanced")).to_have_text("Advanced")
    expect(page.locator('.wf-metrics')).to_have_count(0)
    expect(page.locator('.workflow-preview__node')).to_have_count(4)
    expect(page.locator('.workflow-preview__links path')).to_have_count(3)
    expect(page.locator('.wf-param')).to_have_count(0)
    expect(page.get_by_role('button', name='Open Editor', exact=True)).to_be_visible()
    before = page.evaluate("localStorage.getItem('pymss-studio:workflow-state')")
    original_view = page.locator('.workflow-preview__canvas').get_attribute('viewBox')
    page.get_by_role('button', name='Zoom in preview', exact=True).click()
    assert original_view != page.locator('.workflow-preview__canvas').get_attribute('viewBox')
    page.get_by_role('button', name='Fit view', exact=True).click()
    assert original_view == page.locator('.workflow-preview__canvas').get_attribute('viewBox')
    box = page.locator('.workflow-preview__canvas').bounding_box()
    anchor = {'x': box['x'] + box['width'] * 0.7, 'y': box['y'] + box['height'] * 0.4}
    to_world = """point => {
      const svg = document.querySelector('.workflow-preview__canvas');
      const mapped = new DOMPoint(point.x, point.y).matrixTransform(svg.getScreenCTM().inverse());
      return {x: mapped.x, y: mapped.y};
    }"""
    world_before = page.evaluate(to_world, anchor)
    page.mouse.move(anchor['x'], anchor['y'])
    page.mouse.wheel(0, -120)
    expect(page.locator('.workflow-preview__canvas')).not_to_have_attribute('viewBox', original_view)
    world_after = page.evaluate(to_world, anchor)
    assert abs(world_after['x'] - world_before['x']) < 0.05, (world_before, world_after)
    assert abs(world_after['y'] - world_before['y']) < 0.05, (world_before, world_after)
    zoomed_width = float(page.locator('.workflow-preview__canvas').get_attribute('viewBox').split()[2])
    assert zoomed_width < float(original_view.split()[2])
    page.mouse.wheel(0, 120)
    page.wait_for_function("width => Number(document.querySelector('.workflow-preview__canvas').getAttribute('viewBox').split(' ')[2]) > width", arg=zoomed_width)
    page.get_by_role('button', name='Fit view', exact=True).click()
    page.mouse.move(box['x'] + 70, box['y'] + 70)
    page.mouse.down()
    page.mouse.move(box['x'] + 110, box['y'] + 90)
    page.mouse.up()
    assert original_view != page.locator('.workflow-preview__canvas').get_attribute('viewBox')
    page.get_by_role('button', name='Fit view', exact=True).click()
    assert before == page.evaluate("localStorage.getItem('pymss-studio:workflow-state')")
    page.screenshot(animations='disabled', path=str(output / 'workflows-preview-advanced.png'), full_page=True)

    def check_more_menu(keyboard=False, corner=False):
        button = page.get_by_role('button', name='More actions', exact=True)
        if keyboard:
            button.focus()
            button.press('Enter')
        elif corner:
            box = button.bounding_box()
            button.click(position={'x': box['width'] - 3, 'y': box['height'] - 3})
        else:
            button.click(position={'x': 4, 'y': 4})
        menu = page.locator('.n-dropdown-menu:visible')
        expect(menu).to_have_count(1)
        expect(menu.get_by_text('Delete', exact=True)).to_be_visible()
        page.screenshot(animations='disabled', path=str(output / 'workflows-overview-more-menu.png'), full_page=True)
        bounds = menu.bounding_box()
        anchor = button.bounding_box()
        assert bounds['x'] >= 0 and bounds['y'] >= 40, bounds
        assert bounds['x'] + bounds['width'] <= page.viewport_size['width'] + 1, bounds
        assert bounds['y'] + bounds['height'] <= anchor['y'] + 1, (bounds, anchor)
        assert abs(bounds['x'] + bounds['width'] - anchor['x'] - anchor['width']) <= 2, (bounds, anchor)
        page.keyboard.press('Escape')
        expect(menu).not_to_be_visible()

    check_more_menu()
    check_more_menu(corner=True)
    check_more_menu(keyboard=True)
    page.set_viewport_size({'width': 1024, 'height': 640})
    check_more_menu()
    page.set_viewport_size({'width': 1280, 'height': 850})
    page.locator('.wf-row--simple').click(button='right')
    expect(page.locator('.n-dropdown-menu:visible').get_by_text('Simple Edit', exact=True)).to_be_visible()
    page.get_by_role('button', name='More actions', exact=True).click()
    expect(page.locator('.n-dropdown-menu:visible')).to_have_count(1)
    expect(page.locator('.n-dropdown-menu:visible').get_by_text('Open Advanced Node Editor', exact=True)).to_be_visible()
    page.keyboard.press('Escape')

    page.locator(".wf-type-filter button", has_text="Simple").click()
    expect(page.locator(".wf-row--simple")).to_be_visible()
    expect(page.locator(".wf-row--advanced")).to_have_count(0)
    expect(page.locator(".wf-kind-badge--advanced")).to_have_text("Advanced")

    page.locator(".wf-row--simple").click()
    expect(page.locator(".wf-kind-badge--simple")).to_have_text("Simple")
    expect(page.locator('.workflow-preview__node')).to_have_count(3)
    expect(page.locator('.workflow-preview__links path')).to_have_count(3)
    expect(page.get_by_role('button', name='Open Editor', exact=True)).to_be_visible()
    page.screenshot(animations='disabled', path=str(output / 'workflows-preview-simple.png'), full_page=True)
    page.get_by_role('button', name='Workflow settings', exact=True).click()
    expect(page.locator('.wf-settings .wf-param')).to_have_count(3)
    expect(page.get_by_role('dialog', name='Workflow settings', exact=True)).to_be_visible()
    expect(page.locator('.n-drawer')).to_have_count(0)
    page.screenshot(animations='disabled', path=str(output / 'workflows-settings-modal-open.png'), full_page=True)
    modal = page.locator('.wf-settings-modal').bounding_box()
    assert 600 <= modal['width'] <= 640, modal
    assert abs(modal['x'] + modal['width'] / 2 - 640) < 2, modal
    assert modal['y'] >= 40 and modal['y'] + modal['height'] <= 850, modal
    page.get_by_label('Name', exact=True).fill('Vocal isolation')
    page.get_by_label('Name', exact=True).press('Tab')
    expect(page.locator('.wf-overview__name')).to_have_text('Vocal isolation')
    long_description = '\n\n'.join(f'Line {index}: vocal processing notes and output routing details.' for index in range(35))
    page.get_by_label('Description', exact=True).fill(long_description)
    page.get_by_label('Description', exact=True).press('Tab')
    page.wait_for_function("value => JSON.parse(localStorage.getItem('pymss-studio:workflow-state')).workflows.find(w => w.id === 'simple').description === value", arg=long_description)
    page.locator('.wf-param .n-select').nth(1).click()
    page.get_by_text('FLAC', exact=True).click()
    page.wait_for_function("JSON.parse(localStorage.getItem('pymss-studio:workflow-state')).workflows.find(w => w.id === 'simple').definition.defaults.output_format === 'flac'")
    page.screenshot(animations='disabled', path=str(output / 'workflows-preview-settings.png'), full_page=True)
    page.keyboard.press('Escape')
    expect(page.locator('.wf-settings')).not_to_be_visible()
    summary = page.locator('.wf-overview__description')
    expect(summary).to_have_text(' '.join(long_description.split()))
    assert summary.evaluate('el => el.getBoundingClientRect().height <= parseFloat(getComputedStyle(el).lineHeight) * 2 + 1')
    assert page.locator('.workflow-preview').bounding_box()['height'] >= 300
    assert page.locator('.workflow-preview').bounding_box()['y'] < 350
    expect(page.get_by_role('button', name='Open Editor', exact=True)).to_be_in_viewport()
    page.screenshot(animations='disabled', path=str(output / 'workflows-overview-long-description.png'), full_page=True)
    page.get_by_role('button', name='Workflow settings', exact=True).click()
    expect(page.get_by_label('Description', exact=True)).to_have_value(long_description)
    page.get_by_label('Description', exact=True).fill('A compact two-stem workflow.')
    page.get_by_label('Description', exact=True).press('Tab')
    page.wait_for_function("JSON.parse(localStorage.getItem('pymss-studio:workflow-state')).workflows.find(w => w.id === 'simple').description === 'A compact two-stem workflow.'")
    long_name = 'Long workflow name ' * 30 + 'end'
    page.get_by_label('Name', exact=True).fill(long_name)
    page.get_by_label('Name', exact=True).press('Tab')
    expect(page.locator('.wf-overview__name')).to_have_text(long_name)
    page.keyboard.press('Escape')
    expect(page.locator('.wf-settings')).not_to_be_visible()
    for width, height in [(1280, 850), (1024, 768)]:
        page.set_viewport_size({'width': width, 'height': height})
        heading = page.locator('.wf-overview__name')
        assert heading.evaluate('el => el.getBoundingClientRect().height <= parseFloat(getComputedStyle(el).lineHeight) * 2 + 1')
        expect(heading).to_have_attribute('title', long_name)
        assert page.locator('.workflow-preview').bounding_box()['y'] < 350
        expect(page.get_by_role('button', name='Open Editor', exact=True)).to_be_in_viewport()
    page.screenshot(animations='disabled', path=str(output / 'workflows-overview-long-name.png'), full_page=True)
    page.set_viewport_size({'width': 1280, 'height': 850})
    page.get_by_role('button', name='Workflow settings', exact=True).click()
    expect(page.get_by_label('Name', exact=True)).to_have_value(long_name)
    page.get_by_label('Name', exact=True).fill('Vocal isolation')
    page.get_by_label('Name', exact=True).press('Tab')
    expect(page.locator('.wf-overview__name')).to_have_text('Vocal isolation')
    page.keyboard.press('Escape')
    expect(page.locator('.wf-settings')).not_to_be_visible()
    page.set_viewport_size({'width': 1024, 'height': 640})
    page.get_by_role('button', name='Workflow settings', exact=True).click()
    page.screenshot(animations='disabled', path=str(output / 'workflows-settings-modal-compact.png'), full_page=True)
    compact_modal = page.locator('.wf-settings-modal').bounding_box()
    assert abs(compact_modal['x'] + compact_modal['width'] / 2 - 512) < 2, compact_modal
    assert compact_modal['y'] >= 40 and compact_modal['y'] + compact_modal['height'] <= 640, compact_modal
    assert page.locator('.wf-settings-modal .n-card-content').evaluate('el => el.scrollHeight > el.clientHeight')
    page.keyboard.press('Escape')
    expect(page.locator('.wf-settings')).not_to_be_visible()
    page.set_viewport_size({'width': 1280, 'height': 850})

    page.evaluate("""async definition => {
      const workflow = document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('workflow');
      const entry = await workflow.saveWorkflow({name: 'Branching vocal blend', definition});
      workflow.selectWorkflow(entry.id);
    }""", branching_definition)
    expect(page.locator('.workflow-preview__node')).to_have_count(5)
    page.set_viewport_size({'width': 1920, 'height': 1152})
    page.get_by_role('button', name='Fit view', exact=True).click()
    page.wait_for_function("""() => {
      const canvas = document.querySelector('.workflow-preview__canvas').getBoundingClientRect();
      return [...document.querySelectorAll('.workflow-preview__node > rect')].every(node => {
        const r = node.getBoundingClientRect();
        return r.left >= canvas.left && r.right <= canvas.right && r.top >= canvas.top && r.bottom <= canvas.bottom;
      });
    }""")
    page.screenshot(animations='disabled', path=str(output / 'workflows-preview-branching-fit.png'), full_page=True)

    page.locator(".wf-type-filter button", has_text="All").click()
    page.locator(".wf-row--advanced").click()
    page.set_viewport_size({'width': 1024, 'height': 768})
    expect(page.locator('.workflow-preview')).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    page.screenshot(animations='disabled', path=str(output / 'workflows-preview-compact.png'), full_page=True)

    page.evaluate("document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('settings').themeMode = 'dark'")
    page.wait_for_function("document.documentElement.classList.contains('dark-theme')")
    page.screenshot(animations='disabled', path=str(output / 'workflows-preview-dark.png'), full_page=True)
    page.evaluate("""() => {
      const stores = document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s;
      stores.get('settings').themeMode = 'light';
      stores.get('settings').locale = 'zh-CN';
      stores.get('workflow').selectWorkflow('simple');
    }""")
    page.set_viewport_size({'width': 1280, 'height': 850})
    expect(page.get_by_role('button', name='工作流设置', exact=True)).to_be_visible()
    page.screenshot(animations='disabled', path=str(output / 'workflows-preview-zh-CN.png'), full_page=True)
    page.get_by_role('button', name='工作流设置', exact=True).click()
    page.screenshot(animations='disabled', path=str(output / 'workflows-preview-settings-zh-CN.png'), full_page=True)
    page.get_by_label('名称', exact=True).fill('Vocal isolation revised')
    page.keyboard.press('Escape')
    expect(page.locator('.wf-settings')).not_to_be_visible()
    expect(page.locator('.wf-overview__name')).to_have_text('Vocal isolation revised')
    page.get_by_role('button', name='工作流设置', exact=True).click()
    page.evaluate("document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('workflow').selectWorkflow('advanced')")
    expect(page.locator('.wf-settings')).not_to_be_visible()
    page.evaluate("document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('workflow').nodeEditorOpenWorkflowId = 'advanced'")
    expect(page.locator('.wf-settings-button')).to_be_disabled()
    expect(page.locator('.wf-lock')).to_be_visible()
    page.evaluate("""async () => {
      const workflow = document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('workflow');
      workflow.nodeEditorOpenWorkflowId = '';
      const entry = await workflow.saveWorkflow({name: 'Empty graph', description: '', definition: {nodes: [], links: []}});
      workflow.selectWorkflow(entry.id);
    }""")
    expect(page.locator('.workflow-preview__empty')).to_be_visible()
    page.evaluate("document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('workflow').selectWorkflow('simple')")
    expect(page.locator('.wf-kind-badge--simple')).to_be_visible()
    original = page.evaluate("JSON.parse(JSON.stringify(document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('workflow').selectedWorkflow))")
    count = page.locator('.wf-row').count()
    page.get_by_role('button', name='更多操作', exact=True).click()
    page.locator('.n-dropdown-menu:visible').get_by_text('复制', exact=True).click()
    expect(page.locator('.wf-row')).to_have_count(count + 1)
    duplicate = page.evaluate("JSON.parse(JSON.stringify(document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('workflow').selectedWorkflow))")
    assert duplicate['id'] != original['id'] and duplicate['definition'] == original['definition']
    page.evaluate("""async () => {
      const workflow = document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('workflow');
      const entry = await workflow.saveWorkflow({name: 'Imported node types', definition: {
        nodes: [{id: 1, type: 'constructor'}, {id: 2, type: 'toString'}, {id: 3, type: 'pymss_save_audio'}],
        links: [[1, 1, 0, 2, 0, 'AUDIO'], [2, 2, 0, 3, 0, 'AUDIO']],
      }});
      workflow.selectWorkflow(entry.id);
    }""")
    expect(page.locator('.workflow-preview__node')).to_have_count(3)
    expect(page.locator('.workflow-preview__links path')).to_have_count(2)
    expect(page.locator('.workflow-preview__node-copy strong', has_text='constructor')).to_be_visible()
    expect(page.locator('.workflow-preview__node-copy strong', has_text='toString')).to_be_visible()
    page.add_init_script("Object.defineProperty(navigator, 'platform', { configurable: true, value: 'MacIntel' })")
    page.reload(wait_until='networkidle')
    expect(page.locator('.title-bar')).to_have_count(0)
    page.get_by_role('button', name='Workflow settings', exact=True).click()
    expect(page.locator('.wf-settings')).to_be_visible()
    page.screenshot(animations='disabled', path=str(output / 'workflows-settings-modal-macos.png'), full_page=True)
    modal = page.locator('.wf-settings-modal').bounding_box()
    assert 600 <= modal['width'] <= 640, modal
    assert abs(modal['x'] + modal['width'] / 2 - 640) < 2, modal
    assert modal['y'] >= 0 and modal['y'] + modal['height'] <= 850, modal
    assert not errors, errors
    print(json.dumps({"groups": 2, "filters": 3, "advancedNodes": 4, "simpleNodes": 3, "settingsPersisted": True}))
    print("workflows overview UI smoke passed")
    browser.close()
