"""Check update-source preferences and layout with isolated browser storage."""

import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument("--url", default="http://127.0.0.1:1420")
args = parser.parse_args()
artifacts = Path(__file__).resolve().parents[2] / ".tmp" / "update-sources-ui"
artifacts.mkdir(parents=True, exist_ok=True)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, args=["--no-proxy-server"])
    try:
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script("""if (!localStorage.getItem('pymss-studio:app-settings')) {
          localStorage.setItem('pymss-studio:app-settings', JSON.stringify({startupOnboardingSeen:true, locale:'zh-CN', themeMode:'light', themeAccent:'teal'}));
        }""")
        page.goto(args.url + "/#/")
        page.wait_for_load_state("networkidle")
        page.evaluate("""() => {
          window.__stores = document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s;
          window.__checks = [];
          window.__TAURI_INTERNALS__ = {
            metadata: {currentWindow:{label:'main'}, currentWebview:{label:'main'}}, transformCallback: () => 1,
            invoke: async (command, args) => {
              if (command === 'get_build_info') return {version:'0.0.19', gitCommit:'1df3206ab7e64d03', gitTag:'v0.0.19', runId:'18624073182', runAttempt:'1',
                updateSupported:true, official:true, repository:'pymss-project/pymss-studio', repositoryOwner:'pymss-project', variant:'cpu'};
              if (command === 'check_managed_update') {
                window.__checks.push(args);
                return {currentVersion:'0.0.19', version:'0.0.20', prerelease:false, distribution:'portable', autoUpdateSupported:true,
                  requiresManualInstall:false, source:args.source === 'auto' ? 'modelscope' : args.source, body:'恢复主页工作流推理，完善环境管理与在线更新。'};
              }
              if (command === 'save_app_store') {localStorage.setItem('pymss-studio:' + args.name, JSON.stringify(args.data)); return;}
              if (command === 'load_app_store') return JSON.parse(localStorage.getItem('pymss-studio:' + args.name) || 'null');
              return null;
            },
          };
          window.__stores.get('app').buildInfoUpdateSupported = true;
          window.__stores.get('update').$dispose();
          window.__stores.delete('update');
          location.hash = '/settings';
        }""")
        page.wait_for_load_state("networkidle")
        page.locator(".settings-nav-item").filter(has=page.get_by_text("关于", exact=True)).click()
        source = page.locator(".update-panel__channel").filter(has_text="下载源")
        expect(source).to_contain_text("自动选择")
        expect(page.locator(".update-panel__meta")).to_contain_text("当前来源：魔搭 ModelScope")
        notes = page.locator(".update-panel__release-notes")
        expect(notes).not_to_have_attribute("open", "")
        notes.locator("summary").click()
        expect(notes.locator(".update-panel__notes")).to_be_visible()
        expect(notes.locator(".update-panel__notes")).to_have_text("恢复主页工作流推理，完善环境管理与在线更新。")
        notes.locator("summary").click()
        expect(notes.locator(".update-panel__notes")).to_be_hidden()
        page.locator(".update-panel__channel").first.locator(".update-panel__field-label").hover()
        expect(page.get_by_text("仅接收已正式发布的稳定版本。", exact=True)).to_be_visible()
        page.locator(".section-title").first.hover()

        for value, label in [("modelscope", "魔搭 ModelScope"), ("github", "GitHub")]:
            source.locator(".n-select").click()
            page.locator(".n-base-select-option").filter(has_text=label).click()
            page.wait_for_function("value => window.__checks.at(-1)?.source === value", arg=value)
            expect(source).to_contain_text(label)
            expect(page.locator(".update-panel__meta")).to_contain_text("当前来源：" + label)
            page.wait_for_function("value => JSON.parse(localStorage.getItem('pymss-studio:app-settings')).updateSource === value", arg=value)

        page.keyboard.press("Escape")
        expect(page.locator(".n-base-select-menu:visible")).to_have_count(0)
        source.locator(".n-select").click()
        page.locator(".n-base-select-option").filter(has_text="自动选择").click()
        expect(source).to_contain_text("自动选择")
        page.keyboard.press("Escape")
        page.wait_for_function("window.__checks.at(-1)?.source === 'auto'")
        expect(page.locator(".n-base-select-menu:visible")).to_have_count(0)
        for width in [1280, 1000]:
            page.set_viewport_size({"width": width, "height": 900})
            assert page.locator(".settings-content").evaluate("node => node.scrollWidth <= node.clientWidth + 1")
            controls = page.locator(".update-panel__controls")
            details = page.locator(".update-panel__release-notes")
            height = details.evaluate("node => node.getBoundingClientRect().bottom") - controls.evaluate("node => node.getBoundingClientRect().top")
            assert height < 140, {"width": width, "controlAreaHeight": height}
            if width == 1280:
                page.locator(".about-info-card--update").screenshot(path=str(artifacts / "compact-update-panel.png"))
            page.screenshot(path=str(artifacts / f"sources-{width}.png"), full_page=True)
        page.evaluate("window.__stores.get('settings').themeMode = 'dark'")
        page.screenshot(path=str(artifacts / "sources-dark.png"), full_page=True)
        page.evaluate("window.__stores.get('update').status = 'downloading'")
        expect(page.locator(".update-panel__controls .n-base-selection--disabled")).to_have_count(2)
        page.evaluate("window.__stores.get('update').status = 'available'")
        page.reload()
        page.wait_for_load_state("networkidle")
        assert page.evaluate("document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('settings').updateSource") == "auto"
        assert not errors, errors
        print(json.dumps({"sourceModes": ["auto", "modelscope", "github"], "preferenceRestored": True, "viewports": [1280, 1000]}))
    finally:
        browser.close()
