"""Verify confirmed recovery after a simulated plugin-host failure."""
import argparse
import ast
import json
from pathlib import Path
from playwright.sync_api import expect, sync_playwright
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--url',default='http://127.0.0.1:1438')
parser.add_argument('--output-dir',type=Path,required=True)
args=parser.parse_args()
out=args.output_dir
out.mkdir(parents=True,exist_ok=True)
module=ast.parse(Path(__file__).with_name('editor_vst_ui_smoke.py').read_text('utf-8'))
fixtures={n.targets[0].id:ast.literal_eval(n.value) for n in module.body if isinstance(n,ast.Assign) and isinstance(n.targets[0],ast.Name) and n.targets[0].id in ('MOCK','SETUP')}
results=[]
with sync_playwright() as p:
 browser=p.chromium.launch(headless=True,args=['--no-proxy-server'])
 for action in ('remove','bypass'):
  context=browser.new_context(viewport={'width':1440,'height':900})
  page=context.new_page();page.set_default_timeout(12000)
  errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
  page.add_init_script('('+fixtures['MOCK']+''')(true);
   window.__dead=false;
   const invoke=__TAURI_INTERNALS__.invoke;
   __TAURI_INTERNALS__.invoke=async(command,args={})=>{
     if(command==='plugin_host_capabilities')return {enabled:true,platform:'macos',formats:['vst3','vst2','au'],nativeEditor:true,experimental:true,catalogCache:true};
     if(command==='plugin_host_request'){
       const r=args.request;
       if(r.op==='unload'&&r.discardUnsaved){__dead=false;return {closed:true,superseded:false,generation:7}}
       if(__dead)throw new Error('The audio host exited unexpectedly');
     }
     return invoke(command,args);
   };
  ''')
  page.goto(args.url.rstrip('/')+'/#/editor',wait_until='networkidle')
  page.evaluate(fixtures['SETUP'])
  page.evaluate('''async()=>{
    __editor.session.tracks[0].pluginChain=['good','bad'].map(id=>({id,format:'au',pluginId:'au:aufx:'+id.padEnd(4,'x')+':test',name:id==='bad'?'Bad AU':'Good AU',path:'au:aufx:'+id.padEnd(4,'x')+':test',bypassed:false,state:id==='bad'?'opaque:bad':'sha256:latest-good',extra:{retain:true}}));
    const moduleUrl=performance.getEntriesByType('resource').map(entry=>entry.name).filter(name=>new URL(name).pathname.endsWith('/src/utils/pluginHost.ts')).at(-1);
    const host=await import(moduleUrl);
    await host.ensurePluginSession(__editor.session);
    await __editor.saveProject();
    __editor.session.tracks[0].volume=.72;
    __dead=true;
    try{await host.pluginHostRequest({op:'status',projectId:__editor.session.id})}catch{}
  }''')
  toggle=page.locator('.editor-shell__inspector-toggle')
  if toggle.get_attribute('aria-pressed')=='false':toggle.click()
  page.get_by_role('button',name='音频插件',exact=True).click()
  manager=page.get_by_role('dialog',name='插件管理',exact=True)
  bad=manager.locator('.plugin-panel__slot').filter(has_text='Bad AU')
  def attempt():
   if action=='remove':bad.get_by_role('button',name='移除',exact=True).click()
   else:bad.get_by_role('checkbox').click()
  attempt()
  page.wait_for_timeout(300)
  confirmation=page.locator('.n-dialog').filter(has_text='恢复插件操作')
  expect(confirmation).to_be_visible()
  assert page.evaluate('__editor.session.tracks[0].pluginChain.length')==2
  confirmation.get_by_role('button',name='取消',exact=True).click()
  expect(confirmation).not_to_be_visible()
  assert page.evaluate('__dead') is True
  attempt();expect(confirmation).to_be_visible()
  page.screenshot(path=str(out/f'{action}-confirmation.png'))
  confirmation.get_by_role('button',name='恢复并继续',exact=True).click()
  page.wait_for_function('!__dead && !__editor.pluginStateError')
  data=page.evaluate('''()=>({chain:__editor.session.tracks[0].pluginChain,volume:__editor.session.tracks[0].volume,saved:__savedVstProjects.at(-1)})''')
  assert data['volume']==.72
  assert data['chain'][0]['state']=='sha256:latest-good'
  assert data['chain'][0]['extra']=={'retain':True}
  if action=='remove':assert len(data['chain'])==1
  else:
   assert data['chain'][1]['bypassed'] is True
   assert data['chain'][1]['skipLoad'] is True
   assert data['chain'][1]['state']=='opaque:bad'
  assert data['saved']['tracks'][0]['volume']==.72
  assert not errors,errors
  page.screenshot(path=str(out/f'{action}-recovered.png'))
  results.append({'action':action,'cancelPreservesProject':True,'recovered':True,'retainsCapturedStatesAndMix':True,'pageErrors':errors})
  context.close()
 browser.close()
(out/'report.json').write_text(json.dumps(results,indent=2),'utf-8')
print(json.dumps(results,indent=2))
