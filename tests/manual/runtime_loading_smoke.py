"""Check progressive runtime loading, cached revisits and explicit refreshes."""

import argparse
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument('--url', default='http://127.0.0.1:1448')
parser.add_argument('--output-dir', type=Path, default=Path('.tmp/runtime-loading'))
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, args=['--no-proxy-server'])
    page = browser.new_page(viewport={'width': 1280, 'height': 900})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.add_init_script('''localStorage.setItem('pymss-studio:app-settings', JSON.stringify({
      startupOnboardingSeen:true,locale:'zh-CN',themeMode:'light',animationsEnabled:true
    }))''')
    page.goto(args.url + '/#/settings')
    page.wait_for_load_state('networkidle')
    page.add_style_tag(content='.title-bar {display:none !important}')
    page.evaluate('''() => {
      const stores=document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s;
      window.__runtimeStore=stores.get('app');
      window.__settingsStore=stores.get('settings');
      window.__runtimeCalls=[];
      window.__pendingInfo=[];window.__pendingSizes=[];
      window.__runtimeInfo={platform:'win32',ready:true,installedBackend:'cuda',torchBackend:'cuda',
        torchVersion:'2.7.1+cu128',pymssVersion:'2.1.8',manifestVersion:'2026.10.2',gpuVendors:['nvidia'],
        installedEnvironments:[{backend:'cuda',source:'managed',health:'ready',pythonPath:'D:/runtime/python.exe',
          torchVersion:'2.7.1+cu128',pymssVersion:'2.1.8',pymssCoreVersion:'0.1.11',
          manifestVersion:'2026.10.2',torchBackend:'cuda',acceleratorAvailable:true,pymssGraphAvailable:true}]};
      window.__TAURI_INTERNALS__={metadata:{currentWindow:{label:'main'}},invoke:async(command)=>{
        window.__runtimeCalls.push(command);
        if(command==='runtime_info')return new Promise((resolve,reject)=>window.__pendingInfo.push({resolve,reject}));
        if(command==='runtime_env_sizes')return new Promise(resolve=>window.__pendingSizes.push(resolve));
        if(command==='get_env_info')return {torchAvailable:true,torchBackend:'cuda',pymssVersion:'2.1.8'};
        if(command==='runtime_core_versions')return {packages:{pymss:{latestVersion:'2.1.8'},'pymss-core':{latestVersion:'0.1.11'}}};
        if(command==='get_build_info')return {updateSupported:false};
        if(command==='save_app_store')return;
        throw new Error('Unexpected command: '+command);
      }};
      window.__runtimeStore.runtimeInfo=null;window.__runtimeStore.envInfo=null;
      // An outstanding startup query should be shared by the settings page.
      window.__startupProbe=window.__runtimeStore.checkRuntimeInfo();
    }''')
    page.locator('.settings-nav-item').get_by_text('运行环境', exact=True).click()
    section = page.locator('.runtime-section')
    detect = section.locator('.runtime-hero button')
    expect(section.locator('.runtime-envs-list[aria-busy="true"]')).to_be_visible()
    expect(detect).to_be_disabled()
    page.screenshot(path=str(args.output_dir / 'initial-loading.png'))
    assert page.evaluate("window.__runtimeCalls.filter(c=>c==='runtime_info').length") == 1
    page.evaluate('window.__pendingInfo.shift().resolve(window.__runtimeInfo)')
    page.wait_for_function('window.__pendingSizes.length===1')
    expect(section.locator('.runtime-env-card--skeleton')).to_have_count(0)
    expect(section.locator('.runtime-hero')).to_contain_text('2.7.1+cu128')
    expect(section.locator('.runtime-hero')).to_contain_text('2.1.8')
    expect(section.locator('.runtime-read-status')).to_contain_text('正在统计占用')
    expect(detect).to_be_enabled()
    page.screenshot(path=str(args.output_dir / 'information-ready.png'))

    page.locator('.settings-nav-item').get_by_text('外观', exact=True).click()
    page.locator('.settings-nav-item').get_by_text('运行环境', exact=True).click()
    expect(detect).to_be_enabled()
    assert page.evaluate("window.__runtimeCalls.filter(c=>c==='runtime_info').length") == 1
    assert page.evaluate("window.__runtimeCalls.filter(c=>c==='runtime_env_sizes').length") == 1
    assert page.evaluate("window.__runtimeCalls.filter(c=>c==='get_env_info').length") == 0
    page.evaluate('window.__pendingSizes.shift()({sizes:{cuda:3221225472},incompleteBackends:[]})')
    expect(section.locator('.runtime-read-status')).not_to_contain_text('正在统计占用')
    expect(section.locator('.runtime-env-card--active')).to_contain_text('3.0 GB')

    # Manual detection bypasses caches and includes the detailed device probe.
    detect.click()
    page.wait_for_function('window.__pendingInfo.length===1')
    expect(section.locator('.runtime-env-card--skeleton')).to_have_count(0)
    page.evaluate('window.__pendingInfo.shift().resolve(window.__runtimeInfo)')
    page.wait_for_function('window.__pendingSizes.length===1')
    expect(detect).to_be_enabled()
    assert page.evaluate("window.__runtimeCalls.filter(c=>c==='get_env_info').length") == 1
    page.evaluate('window.__pendingSizes.shift()({sizes:{cuda:3221225472},incompleteBackends:[]})')

    # A failed refresh retains previous information and offers another detection.
    detect.click()
    page.wait_for_function('window.__pendingInfo.length===1')
    page.evaluate("window.__pendingInfo.shift().reject(new Error('Runtime probe failed'))")
    expect(section.locator('.runtime-read-error')).to_have_text('Runtime probe failed')
    expect(section.locator('.runtime-env-card--active')).to_be_visible()
    expect(detect).to_be_enabled()
    page.wait_for_function('window.__pendingSizes.length===1')
    page.evaluate('window.__pendingSizes.shift()({sizes:{cuda:3221225472},incompleteBackends:[]})')
    detect.click()
    page.wait_for_function('window.__pendingInfo.length===1')
    page.evaluate('window.__pendingInfo.shift().resolve(window.__runtimeInfo)')
    expect(section.locator('.runtime-read-error')).to_have_count(0)
    expect(detect).to_be_enabled()
    page.wait_for_function('window.__pendingSizes.length===1')
    page.evaluate('window.__pendingSizes.shift()({sizes:{cuda:3221225472},incompleteBackends:[]})')
    page.evaluate("window.__settingsStore.themeMode='dark'")
    page.set_viewport_size({'width': 1000, 'height': 760})
    assert section.evaluate('el=>el.scrollWidth<=el.clientWidth')
    page.screenshot(path=str(args.output_dir / 'ready-dark.png'))
    page.emulate_media(reduced_motion='reduce')
    assert section.locator('.runtime-value').first.evaluate('el=>getComputedStyle(el).animationName') == 'none'
    assert not errors, errors
    browser.close()

print('PASS: progressive loading, cached revisits, explicit refresh, recovery, dark mode and reduced motion')
