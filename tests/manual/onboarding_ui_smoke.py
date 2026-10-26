"""Verify first-time setup using isolated stores and simulated runtime events."""
import argparse
import re
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--url', default='http://127.0.0.1:1448')
parser.add_argument('--output-dir', type=Path, default=Path('.tmp/onboarding-ui'))
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)


def assert_layout(page):
    expect(page.locator('.viewport-recovery')).not_to_be_visible()
    assert page.locator('.onboarding-card').evaluate('''el => {
      const rect = el.getBoundingClientRect();
      return rect.top >= 0 && rect.bottom <= innerHeight && rect.left >= 0 && rect.right <= innerWidth;
    }''')
    assert page.locator('.onboarding-content').evaluate('el=>el.scrollWidth<=el.clientWidth')
    for button in page.locator('.onboarding-content__foot button').all():
        expect(button).to_be_in_viewport()


def runtime_page(page):
    for _ in range(3):
        page.locator('.onboarding-content__foot .n-button--primary-type').click()
    expect(page.locator('#onboarding-title')).to_have_text('运行准备')


def assert_waveform_labels(page):
    assert page.locator('.onboarding-audio-flow__stems > div').evaluate_all('''rows => rows.every(row => {
      const label=row.querySelector('span').getBoundingClientRect();
      const waveform=row.querySelector('.onboarding-waveform').getBoundingClientRect();
      return label.right+8<=waveform.left && waveform.width>40;
    })'''), 'stem labels overlap their waveforms'


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, args=['--no-proxy-server'])

    def new_page(platform='win32', machine='AMD64', ready=False, models=0):
        page = browser.new_page(viewport={'width': 1366, 'height': 768})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.add_init_script("""localStorage.setItem('pymss-studio:app-settings',JSON.stringify({
          startupOnboardingSeen:false,locale:'zh-CN',themeMode:'dark',animationsEnabled:false
        }))""")
        page.goto(args.url.rstrip('/') + '/index.html', wait_until='networkidle')
        expect(page.locator('.onboarding-card')).to_be_visible()
        page.evaluate('''({platform,machine,ready,models}) => {
          const stores=document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s;
          window.__settings=stores.get('settings');window.__app=stores.get('app');window.__models=stores.get('model');
          window.__calls=[];window.__modelCount=models;
          window.__runtime={platform,machine,ready,installedBackend:platform==='darwin'?'mlx':null,
            torchBackend:'cpu',packages:{mlx:platform==='darwin'},pymssVersion:'2.1.8',
            gpuVendors:platform==='win32'?['nvidia']:[],installedEnvironments:[]};
          __app.checkRuntimeInfo=async()=>{__calls.push('check');__app.runtimeInfo=structuredClone(__runtime)};
          __app.checkEnvInBackground=async()=>{};
          __app.ensureRuntimeInfo=async()=>__app.runtimeInfo;
          __app.installRuntime=async(backend)=>{
            __calls.push('install:'+backend);__app.runtimeInstallBackend=backend;
            __app.runtimeInstallStatus='installing';__app.runtimeInstallMessage='';__app.runtimeInstallLogs=[];
          };
          __app.cancelRuntimeInstall=async()=>{__calls.push('cancel')};
          __app.activateRuntime=async()=>{throw new Error('Unexpected activation')};
          __models.loadModels=async()=>{
            __calls.push('models');__models.models=Array.from({length:__modelCount},(_,i)=>({
              name:'model-'+i,downloaded:true,supported:true,aliases:[],missingPaths:[]
            }));
          };
          window.__TAURI_INTERNALS__={metadata:{currentWindow:{label:'main'},currentWebview:{label:'main'}},
            transformCallback:()=>1,invoke:async()=>null};
          __app.runtimeInfo=structuredClone(__runtime);
          __settings.modelDir=platform==='darwin'?'/Users/Shared/Pymss/Models':'D:/Pymss/Models';
          __settings.outputDir=platform==='darwin'?'/Users/Shared/Pymss/Output':'D:/Pymss/Output';
        }''', {'platform': platform, 'machine': machine, 'ready': ready, 'models': models})
        return page, errors

    page, errors = new_page()
    expect(page.locator('.onboarding-nav__item')).to_have_count(4)
    expect(page.locator('.onboarding-project__notice')).to_contain_text('免费开源')
    expect(page.locator('.onboarding-project__notice')).to_contain_text('均与项目官方无关')
    expect(page.locator('.app-body')).to_have_attribute('inert', '')
    assert_layout(page)
    assert_waveform_labels(page)
    page.evaluate("__settings.locale='en'")
    expect(page.locator('#onboarding-title')).to_have_text('Welcome to Pymss Studio')
    for width, height in [(1280, 850), (1024, 600), (760, 600)]:
        page.set_viewport_size({'width': width, 'height': height})
        assert_layout(page)
        assert_waveform_labels(page)
    page.set_viewport_size({'width': 1366, 'height': 768})
    page.screenshot(path=str(args.output_dir / 'welcome-en.png'))
    page.evaluate("__settings.locale='zh-CN'")
    page.screenshot(path=str(args.output_dir / 'welcome-dark.png'))
    page.locator('.onboarding-content__foot .n-button--primary-type').click()
    expect(page.locator('.onboarding-preview')).to_be_visible()
    expect(page.locator('.onboarding-preview .onboarding-waveform')).to_have_count(0)
    page.locator('.onboarding-chip').filter(has_text='浅色').click()
    expect(page.locator('html')).to_have_class('light-theme')
    before = page.locator('.onboarding-preview__action').evaluate('el=>getComputedStyle(el).backgroundColor')
    page.locator('.onboarding-accent-card').last.click()
    expect(page.locator('.onboarding-accent-card').last).to_have_attribute('aria-pressed', 'true')
    assert page.locator('.onboarding-preview__action').evaluate('el=>getComputedStyle(el).backgroundColor') != before
    page.screenshot(path=str(args.output_dir / 'appearance-light.png'))
    for width, height in [(1366, 768), (1024, 600), (820, 600), (760, 600)]:
        page.set_viewport_size({'width': width, 'height': height})
        assert_layout(page)
    page.evaluate("__settings.locale='en'")
    expect(page.locator('#onboarding-title')).to_have_text('Appearance')
    assert_layout(page)
    page.screenshot(path=str(args.output_dir / 'appearance-en-compact.png'))
    page.evaluate("__settings.locale='zh-CN'")
    page.set_viewport_size({'width': 1024, 'height': 600})
    page.locator('.onboarding-content__foot .n-button--primary-type').click()
    expect(page.locator('.onboarding-directory')).to_have_count(2)
    expect(page.locator('.onboarding-directories')).to_contain_text('D:/Pymss/Models')
    assert_layout(page)
    page.locator('.onboarding-content__foot .n-button--primary-type').click()
    expect(page.locator('.onboarding-runtime-card')).to_have_count(3)
    expect(page.locator('.onboarding-runtime-card').filter(has_text='NVIDIA CUDA')).to_have_attribute('aria-pressed', 'true')
    page.locator('.onboarding-runtime-card').filter(has_text='CPU').click()
    expect(page.locator('.onboarding-runtime-mirror')).to_have_count(0)
    expect(page.locator('.onboarding-runtime-logs')).to_have_count(0)
    assert_layout(page)
    page.screenshot(path=str(args.output_dir / 'windows-runtime-handoff.png'))
    page.get_by_role('button', name='前往环境管理', exact=True).click()
    expect(page.locator('.onboarding-card')).to_have_count(0)
    expect(page.locator('.runtime-section')).to_be_visible()
    expect(page.locator('[data-runtime-backend="cpu"]').first).to_have_class(re.compile('runtime-env-card--requested'))
    assert page.evaluate("location.hash.includes('section=runtime')&&location.hash.includes('backend=cpu')")
    assert page.evaluate('__settings.startupOnboardingSeen')
    assert not page.evaluate("__calls.some(call=>call.startsWith('install:'))")
    page.screenshot(path=str(args.output_dir / 'windows-runtime-management.png'))
    assert not errors, errors
    page.close()

    page, errors = new_page()
    page.evaluate("__app.runtimeInstallStatus='installing';__app.runtimeInstallBackend='cpu'")
    runtime_page(page)
    page.locator('.onboarding-runtime-card').filter(has_text='CPU').click()
    page.get_by_role('button', name='前往环境管理', exact=True).click()
    expect(page.locator('.onboarding-card')).to_have_count(0)
    assert page.evaluate("__app.runtimeInstallStatus==='installing'&&!__calls.includes('cancel')")
    page.evaluate("document.querySelector('#app').__vue_app__.config.globalProperties.$router.push({name:'models'})")
    page.wait_for_function("location.hash==='#/models'")
    assert page.evaluate("__app.runtimeInstallStatus==='installing'")
    assert not errors, errors
    page.close()

    page, errors = new_page(ready=True, models=1)
    runtime_page(page)
    page.locator('.onboarding-runtime-card').filter(has_text='CPU').click()
    expect(page.get_by_role('button', name='开始使用', exact=True)).to_be_enabled()
    expect(page.locator('.onboarding-readiness > div')).to_have_count(1)
    assert not page.evaluate("__calls.includes('models')")
    page.screenshot(path=str(args.output_dir / 'windows-ready.png'))
    page.get_by_role('button', name='开始使用', exact=True).click()
    expect(page.locator('.onboarding-card')).to_have_count(0)
    assert page.evaluate("location.hash==='#/'&&__settings.startupOnboardingSeen")
    assert not errors, errors
    page.close()

    for machine in ['arm64', 'x86_64']:
        page, errors = new_page('darwin', machine, ready=True)
        runtime_page(page)
        expect(page.locator('.onboarding-runtime-single')).to_contain_text('Apple MLX')
        expect(page.locator('.onboarding-runtime-grid')).to_have_count(0)
        expect(page.locator('.onboarding-runtime-mirror')).to_have_count(0)
        expect(page.get_by_role('button', name='安装所选环境', exact=True)).to_have_count(0)
        page.set_viewport_size({'width': 1024, 'height': 600})
        assert_layout(page)
        page.screenshot(path=str(args.output_dir / f'macos-{machine}-ready.png'))
        expect(page.locator('.onboarding-readiness > div')).to_have_count(1)
        assert not page.evaluate("__calls.includes('models')")
        page.evaluate("__runtime.ready=false;__app.runtimeInfo=structuredClone(__runtime)")
        expect(page.get_by_role('button', name='重新检测', exact=True)).to_be_enabled()
        expect(page.get_by_role('button', name='前往环境管理', exact=True)).to_be_enabled()
        assert not page.evaluate("__calls.some(call=>call.startsWith('install:'))")
        page.evaluate("__runtime.ready=true;__app.runtimeInfo=structuredClone(__runtime)")
        page.get_by_role('button', name='开始使用', exact=True).click()
        expect(page.locator('.onboarding-card')).to_have_count(0)
        assert page.evaluate("location.hash==='#/'&&__settings.startupOnboardingSeen")
        assert not errors, errors
        page.close()

    for platform, machine, backend, cold, animated in [
        ('win32', 'AMD64', 'rocm', False, True),
        ('win32', 'AMD64', 'rocm', True, True),
        ('win32', 'AMD64', 'rocm', True, False),
        ('darwin', 'arm64', 'mlx', True, True),
    ]:
        page, errors = new_page(platform, machine)
        page.set_viewport_size({'width': 1024, 'height': 720})
        page.evaluate('''({cold,animated}) => {
          __settings.animationsEnabled=animated;
          __settings.startupOnboardingSeen=true;
          __app.ensureRuntimeEnvSizes=async()=>{};
          __app.loadRuntimeEnvSizes=async()=>{};
          __app.ensureRuntimeCoreVersions=async()=>{};
          __app.loadRuntimeCoreVersions=async()=>{};
          window.__backendScrolls=[];
          const original=HTMLElement.prototype.scrollIntoView;
          HTMLElement.prototype.scrollIntoView=function(options){
            if(this.dataset.runtimeBackend)__backendScrolls.push(this.dataset.runtimeBackend);
            return original.call(this,options);
          };
          __app.runtimeInfo=cold?null:structuredClone(__runtime);
          __app.runtimeInfoLoading=cold;
          const read=cold?new Promise(resolve=>window.__finishRuntimeRead=()=>{
            __app.runtimeInfo=structuredClone(__runtime);
            __app.runtimeInfoLoading=false;
            resolve(__app.runtimeInfo);
          }):Promise.resolve(__app.runtimeInfo);
          __app.ensureRuntimeInfo=()=>read;
          __app.checkRuntimeInfo=()=>read;
        }''', {'cold': cold, 'animated': animated})
        page.evaluate('''backend=>document.querySelector('#app').__vue_app__.config.globalProperties.$router.push({
          name:'settings',query:{section:'runtime',backend}
        })''', backend)
        expect(page.locator('.runtime-section')).to_be_visible()
        if cold:
            expect(page.locator('.runtime-env-card--skeleton').first).to_be_visible()
            page.evaluate('__finishRuntimeRead()')
        card=page.locator(f'[data-runtime-backend="{backend}"]').first
        expect(card).to_be_visible()
        page.wait_for_function("!document.querySelector('.runtime-content-enter-active,.runtime-content-leave-active')")
        assert page.evaluate('__backendScrolls') == [backend], (platform, cold, animated, page.evaluate('__backendScrolls'))
        expect(card).to_be_in_viewport()
        assert not errors, errors
        page.close()

    browser.close()

print('PASS: four steps, themes, locales, compact layouts, runtime-management handoff, cold-loading backend positioning, background navigation, and macOS MLX-only readiness')
