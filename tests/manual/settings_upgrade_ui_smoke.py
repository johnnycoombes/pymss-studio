"""Check saved tutorial progress and audio preferences across settings upgrades."""
import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--url', default='http://127.0.0.1:1487')
parser.add_argument('--output-dir', type=Path, default=Path('.tmp/settings-upgrade-ui'))
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, args=['--no-proxy-server'])
    try:
        def open_settings(language, status='active', step_id='toolsOverview', parameter=None):
            context = browser.new_context(viewport={'width': 1280, 'height': 850})
            progress = {'status': status, 'step': 0, 'stepId': step_id}
            if parameter:
                progress['parameter'] = parameter
            state = {'startupOnboardingSeen': True, 'beginnerGuideInvitationSeen': True,
                     'locale': language, 'themeMode': 'light' if language == 'zh-CN' else 'dark',
                     'animationsEnabled': False, 'mp3BitRate': '512k', 'm4aBitRate': '512k',
                     'beginnerGuideProgress': progress}
            context.add_init_script("""if (window.parent === window && !sessionStorage.getItem('settings-upgrade-seeded')) {
              localStorage.setItem('pymss-studio:app-settings', %s);
              sessionStorage.setItem('settings-upgrade-seeded', '1');
            }""" % json.dumps(json.dumps(state)))
            page = context.new_page()
            page.goto(args.url.rstrip('/') + '/#/settings?section=about', wait_until='networkidle')
            page.evaluate("window.__stores=document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s")
            expect(page.locator('[data-guide="guide-help"]')).to_be_visible()
            messages = json.loads(Path('src/i18n', language + '.json').read_text(encoding='utf-8'))['beginnerGuide']
            return context, page, messages

        def tutorial_frame(page, step_id):
            page.wait_for_function("document.querySelector('.tutorial-data-host iframe') && !document.querySelector('.tutorial-data-host__loading')")
            frame = page.locator('.tutorial-data-host iframe').element_handle().content_frame()
            frame.wait_for_function("id=>{const guide=document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('beginnerGuide');return guide.currentStep.id===id&&guide.phase==='running'&&!guide.navigating}", arg=step_id)
            if step_id != 'encoding':
                expect(frame.locator('.beginner-guide-coach')).to_be_visible()
            return frame

        for language in ['zh-CN', 'en']:
            context, page, messages = open_settings(language)
            expect(page.locator('.beginner-guide-invite')).to_have_count(0)
            expect(page.get_by_role('button', name=messages['resume'], exact=True)).to_be_visible()
            page.wait_for_function("JSON.parse(localStorage.getItem('pymss-studio:app-settings')).mp3BitRate==='320k'")
            assert page.evaluate("__stores.get('settings').getAudioParams().mp3_bit_rate") == '320k'
            assert page.evaluate("JSON.parse(localStorage.getItem('pymss-studio:app-settings')).m4aBitRate") == '512k'
            page.reload(wait_until='networkidle')
            expect(page.get_by_role('button', name=messages['resume'], exact=True)).to_be_visible()
            assert page.evaluate("document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('settings').beginnerGuideProgress.stepId") == 'toolsOverview'
            page.set_viewport_size({'width': 1024, 'height': 600})
            page.locator('[data-guide="guide-help"]').scroll_into_view_if_needed()
            page.screenshot(path=str(args.output_dir / (language + '-continue-guide.png')))
            page.get_by_role('button', name=messages['resume'], exact=True).click()
            tutorial_frame(page, 'toolsOverview')
            print('Verified saved progress, preferences, reload and manual resume:', language, flush=True)
            context.close()

        for parameter, output_format in [('wavBitDepth', 'wav'), ('flacBitDepth', 'flac'), ('mp3BitRate', 'mp3'), ('m4aBitRate', 'm4a'), ('m4aCodec', 'm4a')]:
            context, page, messages = open_settings('zh-CN', step_id='encoding', parameter=parameter)
            original_format = page.evaluate("__stores.get('settings').defaultFormat")
            page.get_by_role('button', name=messages['resume'], exact=True).click()
            frame = tutorial_frame(page, 'encoding')
            expect(frame.locator('.parameter-field-guide[data-parameter="' + parameter + '"]')).to_be_visible()
            assert frame.evaluate("document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('settings').defaultFormat") == output_format
            page.wait_for_function("parameter=>JSON.parse(localStorage.getItem('pymss-studio:app-settings')).beginnerGuideProgress.parameter===parameter", arg=parameter)
            assert page.evaluate("__stores.get('settings').defaultFormat") == original_format
            print('Verified parameter-level tutorial resume:', parameter, output_format, flush=True)
            context.close()

        context, page, messages = open_settings('zh-CN')
        page.get_by_role('button', name=messages['restart'], exact=True).click()
        tutorial_frame(page, 'workspace')
        print('Verified explicit restart begins at the first step', flush=True)
        context.close()

        for status in ['skipped', 'completed']:
            context, page, messages = open_settings('zh-CN', status=status)
            expect(page.get_by_role('button', name=messages['resume'], exact=True)).to_have_count(0)
            expect(page.get_by_role('button', name=messages['restart'], exact=True)).to_be_visible()
            print('Verified tutorial actions for status:', status, flush=True)
            context.close()
    finally:
        browser.close()
