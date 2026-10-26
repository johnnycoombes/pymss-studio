"""Verify master-only limiter controls and real Chromium AudioWorklet rendering."""
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
results = []
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, args=['--no-proxy-server'])
    for theme in ('light', 'dark'):
        context = browser.new_context(viewport={'width': 1400, 'height': 900})
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        mock = fixtures['MOCK'].replace("themeMode:'light'", 'themeMode:' + repr(theme))
        page.add_init_script(f'({mock})(true)')
        page.goto(args.url.rstrip('/') + '/#/editor', wait_until='networkidle')
        page.evaluate(fixtures['SETUP'])
        toggle = page.locator('.editor-shell__inspector-toggle')
        if toggle.get_attribute('aria-pressed') == 'false':
            toggle.click()
        inspector = page.locator('.editor-inspector')
        panel = inspector.locator('[data-testid="master-limiter"]')
        expect(panel).to_have_count(0)
        inspector.get_by_role('button', name='主输出', exact=True).click()
        expect(panel).to_be_visible()
        switch = panel.get_by_role('switch', name='启用限幅器')
        expect(switch).to_have_attribute('aria-checked', 'false')
        inputs = panel.locator('input')
        expect(inputs.nth(0)).to_have_value('3.0')
        expect(inputs.nth(1)).to_have_value('250')
        expect(inputs.nth(0)).to_be_disabled()
        switch.click()
        expect(inputs.nth(0)).to_be_enabled()
        inputs.nth(1).fill('500')
        inputs.nth(1).press('Tab')
        slider = panel.get_by_role('slider').first
        slider.focus()
        slider.press('ArrowRight')
        page.wait_for_function('__editor.session.masterLimiter.gainDb > 0')
        switch.click()
        page.wait_for_function('__editor.session.masterLimiter.enabled === false')
        expect(inputs.nth(1)).to_have_value('500')
        page.evaluate('async () => { await __editor.saveProject(); await __editor.loadProject(__editor.session.id) }')
        inspector.get_by_role('button', name='主输出', exact=True).click()
        expect(panel.get_by_role('switch')).to_have_attribute('aria-checked', 'false')
        expect(panel.locator('input').nth(1)).to_have_value('500')
        for width, height in ((1400, 900), (980, 680)):
            page.set_viewport_size({'width': width, 'height': height})
            panel.scroll_into_view_if_needed()
            page.wait_for_timeout(100)
            assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
            times = panel.locator('.master-limiter__times label')
            first, second = times.nth(0).bounding_box(), times.nth(1).bounding_box()
            assert abs(first['y'] - second['y']) < 1, 'time controls must share the top row'
            assert first['x'] + first['width'] <= second['x']
            page.screenshot(path=str(args.output_dir / f'{theme}-{width}x{height}.png'))
        assert not errors, errors
        results.append({'theme': theme, 'ui': 'passed'})
        if theme == 'light':
            result = page.evaluate("""async () => {
              const {default: url} = await import('/src/audio/masterLimiterProcessor.js?url');
              const {MasterLimiterDSP} = await import('/src/audio/masterLimiterProcessor.js');
              const config = {enabled:true,gainDb:12,ceilingDb:-6,attackMs:3,releaseMs:250};
              const rate=48000, length=4800, delay=Math.ceil(rate*config.attackMs/1000);
              const ctx=new OfflineAudioContext(2,length+delay,rate);
              await ctx.audioWorklet.addModule(url);
              const limiter=new AudioWorkletNode(ctx,'pymss-master-limiter',{
                numberOfInputs:1,numberOfOutputs:1,outputChannelCount:[2],
                channelCount:2,channelCountMode:'explicit',processorOptions:config,
              });
              const buffer=ctx.createBuffer(2,length,rate);
              for(let i=0;i<length;i++) {
                const value=i===0||i===length-1?4:Math.sin(i*0.03)*0.1;
                buffer.getChannelData(0)[i]=value;
                buffer.getChannelData(1)[i]=-value*0.25;
              }
              const source=ctx.createBufferSource(); source.buffer=buffer;
              source.connect(limiter); limiter.connect(ctx.destination); source.start();
              const rendered=await ctx.startRendering();
              const l=rendered.getChannelData(0),r=rendered.getChannelData(1);
              const expectedL=new Float32Array(length+delay),expectedR=new Float32Array(length+delay);
              new MasterLimiterDSP(rate,config).process(buffer.getChannelData(0),buffer.getChannelData(1),expectedL,expectedR);
              let peak=0,error=0,stereoError=0;
              for(let i=0;i<l.length;i++) {
                peak=Math.max(peak,Math.abs(l[i]));error=Math.max(error,Math.abs(l[i]-expectedL[i]));
                stereoError=Math.max(stereoError,Math.abs(r[i]+l[i]*0.25));
              }
              limiter.port.close();limiter.disconnect();
              return {peak,error,stereoError,frames:l.length,first:l[delay],last:l.at(-1)};
            }""")
            assert result['peak'] <= 10 ** (-6 / 20) + 1e-7, result
            assert result['error'] < 1e-7 and result['stereoError'] < 1e-7, result
            assert result['frames'] == 4944 and abs(result['first']) > 0 and abs(result['last']) > 0, result
            results.append({'audioWorklet': result})
        context.close()
    browser.close()
(args.output_dir / 'report.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), 'utf-8')
print(json.dumps(results, ensure_ascii=False, indent=2))
