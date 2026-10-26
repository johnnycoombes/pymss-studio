"""Verify structured log provenance, colors, details and history scrolling."""
import argparse
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--url", default="http://127.0.0.1:1433")
parser.add_argument("--output-dir", type=Path, default=Path(".tmp/structured-logs-ui"))
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, args=["--no-proxy-server"])
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script("""localStorage.setItem('pymss-studio:app-settings', JSON.stringify({
      startupOnboardingSeen:true,locale:'zh-CN',themeMode:'light',developerMode:false
    }))""")
    page.goto(args.url.rstrip("/") + "/#/debug", timeout=60000)
    page.wait_for_load_state("networkidle")
    page.evaluate("""async () => {
      const stores=document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s;
      window.__settings=stores.get('settings'); window.__tasks=stores.get('task'); window.__copied='';
      Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>window.__copied=text}});
      window.__records=[
        ['studio','info','studio.app','Application ready'],
        ['worker','info','worker.workflow','Workflow started'],
        ['pymss','info','pymss.separator','Using device: cuda:0'],
        ['pymss','warn','pymss.separator','Model fallback enabled'],
        ['third_party',null,'pip','Installing dependencies'],
        ['pymss','error','pymss.separator','CUDA allocation failed'],
      ].map(([source,level,logger,message],i)=>({schemaVersion:1,id:'log-'+i,
        timestamp:'2026-10-19T12:56:40.123Z',source,level,logger,event:'logging.record',message,
        taskId:'workflow-42',nodeId:i>1?'step-7':undefined,context:{command:'infer_workflow',model:'model.ckpt'},
        location:i===5?{file:'pymss/separator.py',line:1250}:undefined,
        detail:i===5?'Traceback (most recent call last):\\n  File "separator.py", line 1250\\nRuntimeError: CUDA out of memory':undefined}));
      __records[5].message+=': '+ 'Additional decoder context '.repeat(30);
      window.__logText=__records.map(record=>JSON.stringify(record)).join('\\n');
      window.__TAURI_INTERNALS__={metadata:{currentWindow:{label:'main'},currentWebview:{label:'main'}},
        transformCallback:()=>1,invoke:async(command,args)=>{
          if(command==='debug_log_info')return {path:'D:/logs/current-session.log',persistentPath:'D:/logs/pymss-studio.log',
            exists:true,persistentExists:true,sizeBytes:1000,persistentSizeBytes:1000,maxBytes:5242880,persistentMaxBytes:10485760};
          if(command==='debug_log_read')return {path:'D:/logs/pymss-studio.log',content:__logText,sizeBytes:__logText.length,truncated:false};
          if(command==='save_app_store'||command==='write_frontend_log')return;
          if(command==='get_build_info')return {version:'0.0.12',variant:'default',updateSupported:false};
          if(command==='get_workflow_editors')return {revision:1,editors:[]};
          if(command==='plugin:event|listen'||command==='plugin:event|unlisten')return 1;
          if(command==='optional_runtime_package_status')return {package:'funasr',installed:false,present:false};
          if(command==='runtime_info')return {ready:true,packages:{},installedEnvironments:[]};
          if(command==='get_env_info')return {pythonVersion:'3.12',pymssAvailable:true,torchAvailable:true};
          if(command==='get_app_paths')return {dataRoot:'D:/data',logsDir:'D:/logs'};
          throw new Error('Unexpected command: '+command);
        }};
      __settings.developerMode=true;
    }""")
    page.locator(".debug-tabs").get_by_role("button", name="日志", exact=True).click()
    rows = page.locator(".debug-log-row")
    expect(rows).to_have_count(6)
    expect(rows.nth(2).locator(".debug-log-row__source")).to_have_text("pymss")
    expect(rows.nth(2).locator(".debug-log-row__message")).to_contain_text("Using device: cuda:0")
    expect(rows.nth(2).locator(".debug-log-row__message")).to_contain_text("command=infer_workflow")
    expect(rows.nth(2).locator(".debug-log-row__message-text")).to_have_text("Using device: cuda:0")
    expect(rows.nth(2).locator(".debug-log-row__message-context")).to_have_text(" · command=infer_workflow · model=model.ckpt")
    message_styles = rows.nth(2).evaluate("""row => {
      const message=getComputedStyle(row.querySelector('.debug-log-row__message-text'));
      const context=getComputedStyle(row.querySelector('.debug-log-row__message-context'));
      return {weight:message.fontWeight,contextWeight:context.fontWeight,
        color:message.color,contextColor:context.color,fontFamily:message.fontFamily,
        synthesis:message.fontSynthesis};
    }""")
    assert message_styles["weight"] == message_styles["contextWeight"] == "400", message_styles
    assert message_styles["color"] != message_styles["contextColor"], message_styles
    assert message_styles["synthesis"] == "none", message_styles
    assert rows.nth(5).locator(".debug-log-row__message").evaluate("el=>getComputedStyle(el).textOverflow==='ellipsis' && el.scrollWidth>el.clientWidth")
    expect(rows.nth(4).locator(".debug-log-row__level")).to_have_text("未分级")
    colors = rows.evaluate_all("elements=>elements.map(el=>getComputedStyle(el.querySelector('.debug-log-row__source')).color)")
    assert len(set(colors)) == 4, colors
    assert rows.nth(3).locator(".debug-log-row__level").evaluate("el=>getComputedStyle(el).color") != rows.nth(5).locator(".debug-log-row__level").evaluate("el=>getComputedStyle(el).color")
    rows.nth(5).focus()
    page.keyboard.press("Enter")
    expect(rows.nth(5).locator(".debug-log-row__detail")).to_contain_text("RuntimeError: CUDA out of memory")
    expect(rows.nth(5).locator(".debug-log-row__location-detail")).to_contain_text("separator.py:1250")
    expect(rows.nth(5).locator(".debug-log-row__full-message pre")).to_have_text(page.evaluate("__records[5].message"))
    expect(rows.nth(5).locator(".debug-log-row__raw pre")).to_be_visible()
    page.screenshot(path=str(args.output_dir / "structured-light.png"), full_page=True)
    page.evaluate("__settings.themeMode='dark'")
    page.wait_for_timeout(350)
    page.screenshot(path=str(args.output_dir / "structured-dark.png"), full_page=True, animations="disabled")
    assert rows.nth(2).locator(".debug-log-row__message-text").evaluate("el=>getComputedStyle(el).fontWeight") == "400"
    page.get_by_role("button", name="复制筛选日志", exact=True).click()
    assert "Using device: cuda:0" in page.evaluate("__copied")
    assert '"command": "infer_workflow"' in page.evaluate("__copied")
    assert '"model": "model.ckpt"' in page.evaluate("__copied")
    page.evaluate("""() => {
      __logText=Array.from({length:120},(_,i)=>JSON.stringify({...__records[i%6],id:'long-'+i,message:'Record '+i})).join('\\n');
    }""")
    page.locator(".debug-log-toolbar").get_by_role("button", name="刷新", exact=True).click()
    expect(rows).to_have_count(120)
    page.locator(".debug-log-stream").evaluate("el=>el.scrollTop=0")
    page.locator(".debug-log-toolbar").get_by_role("button", name="刷新", exact=True).click()
    assert page.locator(".debug-log-stream").evaluate("el=>el.scrollTop") == 0
    page.set_viewport_size({"width": 900, "height": 850})
    assert page.locator(".debug-log-stream").evaluate("el=>el.getBoundingClientRect().width") < 900
    page.screenshot(path=str(args.output_dir / "structured-narrow.png"), full_page=True)
    assert not errors, errors
    browser.close()
print("PASS: source and level colors, message typography and context hierarchy, keyboard details, themes, copy and preserved scrolling")
