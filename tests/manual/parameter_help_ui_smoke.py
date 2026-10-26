"""Validate parameter diagrams and topic navigation in the real parameter help viewer."""
import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--url', default='http://localhost:1420')
parser.add_argument('--output-dir', type=Path, default=Path('.tmp/parameter-diagrams-ui'))
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)
topics = ['chunk', 'overlap', 'tta', 'batch', 'standardize', 'normalize', 'bitDepth', 'bitRate', 'vrWindow', 'vrAggression', 'vrTta']

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    for language, theme, width in [('zh-CN', 'light', 1280), ('en', 'dark', 1024)]:
        messages = json.loads(Path('src/i18n', language + '.json').read_text(encoding='utf-8'))['parameterHelp']
        page = browser.new_page(viewport={'width': width, 'height': 850})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        settings = json.dumps({'startupOnboardingSeen': True, 'beginnerGuideInvitationSeen': True, 'locale': language, 'themeMode': theme,
                              'animationsEnabled': False, 'beginnerGuideProgress': {'status': 'skipped', 'step': 0}})
        page.add_init_script('localStorage.setItem("pymss-studio:app-settings",' + json.dumps(settings) + ')')
        page.goto(args.url + '/#/settings?section=about')
        page.wait_for_load_state('networkidle')
        page.locator('[data-guide="guide-help"]').get_by_role('button').last.click()
        lesson = page.locator('.parameter-lesson')
        expect(lesson).to_be_visible()
        for topic in topics:
            title = messages[topic + 'Title']
            lesson.locator('.parameter-lesson__topic-menu').click()
            page.locator('.n-dropdown-menu').get_by_text(title, exact=True).click()
            expect(page.locator('.n-dropdown-menu').last).to_be_hidden()
            expect(lesson.locator('h2')).to_have_text(title)
            expect(lesson.locator('svg[role="img"]')).to_be_visible()
            if topic in ['tta', 'vrTta']:
                diagram = lesson.locator('.audio-diagram')
                expect(diagram.locator('svg')).to_contain_text(messages['diagram']['passes'].replace('{count}', '2' if topic == 'vrTta' else '3'))
                diagram.get_by_role('button', name=messages['diagram']['disabled'], exact=True).click()
                expect(diagram.locator('svg')).to_contain_text(messages['diagram']['passes'].replace('{count}', '1'))
                diagram.get_by_role('button', name=messages['diagram']['enabled'], exact=True).click()
            if topic == 'batch':
                expect(lesson.locator('.batch-block--active')).to_have_count(1)
                lesson.locator('.diagram-options button').last.click()
                expect(lesson.locator('.batch-block--active')).to_have_count(4)
            if topic == 'bitRate':
                expect(lesson.locator('.bit-rate-options button[aria-pressed="true"]')).to_have_text('320 kbps')
                expect(lesson.locator('.bit-rate-options button')).to_have_text(['128 kbps', '192 kbps', '320 kbps'])
                lesson.locator('.bit-rate-options button').first.click()
                expect(lesson.locator('svg[role="img"]')).to_contain_text('0.96 MB')
                lesson.locator('.bit-rate-options button').last.click()
                expect(lesson.locator('svg[role="img"]')).to_contain_text('2.40 MB')
                lesson.locator('.bit-rate-codecs button').filter(has_text='AAC').click()
                expect(lesson.locator('.bit-rate-options button[aria-pressed="true"]')).to_have_text('320 kbps')
                expect(lesson.locator('.bit-rate-options button')).to_have_text(['128 kbps', '192 kbps', '320 kbps', '512 kbps'])
                lesson.locator('.bit-rate-options button').last.click()
                expect(lesson.locator('svg[role="img"]')).to_contain_text('3.84 MB')
                lesson.locator('.bit-rate-codecs button').filter(has_text='MP3').click()
                expect(lesson.locator('.bit-rate-options button[aria-pressed="true"]')).to_have_text('320 kbps')
            page.wait_for_function('''()=>{
              const svg=document.querySelector('.parameter-lesson svg[role="img"]');
              return [...svg.querySelectorAll('text')].every(text=>{
                const box=text.getBBox();return box.x>=-1&&box.x+box.width<=637;
              });
            }''')
            lesson.screenshot(path=str(args.output_dir / (language + '-' + topic + '.png')))
        lesson.get_by_role('button', name=messages['done'], exact=True).click()
        expect(lesson).to_have_count(0)
        assert not errors, errors
        print('Passed parameter diagrams:', language, theme, width, flush=True)
        page.close()
    browser.close()
