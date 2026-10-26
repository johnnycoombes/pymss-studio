"""Check that native render events update the export dialog before encoding begins."""
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
mock = fixtures['MOCK'].replace('const callbacks=new Map(),listeners=new Map(); let next=0;', """
  const callbacks=new Map(),listeners=new Map(); let next=0;
  window.__emitWorkerEvent=payload=>{for(const listener of listeners.values())if(listener.event==='pymss://worker-event')
    listener.callback({event:listener.event,id:1,payload})};
""")
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, args=['--no-proxy-server'])
    context = browser.new_context(viewport={'width': 1280, 'height': 900})
    page = context.new_page()
    page.set_default_timeout(15000)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.add_init_script(f"""({mock})(true);
      const invoke=__TAURI_INTERNALS__.invoke;
      __TAURI_INTERNALS__.invoke=async(command,args={{}})=>{{
        if(command==='plugin_host_render'){{
          window.__renderTask=args.taskId;
          return new Promise((resolve,reject)=>{{
            window.__finishRender=()=>resolve({{path:'C:/Audio/render.wav',duration:10,sampleRate:48000,channels:2}});
            window.__rejectRender=reject;
          }});
        }}
        if(command==='start_editor_mix_export'){{window.__encodeTask=args.payload.taskId;return{{taskId:args.payload.taskId,started:true}}}}
        if(command==='plugin_host_cancel_render'){{__rejectRender(new Error('Rendering cancelled'));return true}}
        return invoke(command,args);
      }};
    """)
    page.goto(args.url.rstrip('/') + '/#/editor', wait_until='networkidle')
    page.evaluate(fixtures['SETUP'])
    page.evaluate("""() => {
      const settings=document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('settings');
      settings.outputDir='C:/Audio/Exports';
      __editor.session.sources=[{id:'source',path:'C:/Audio/source.wav',name:'Source',role:'stem',duration:10,
        sampleRate:48000,channels:2,peaks:[0.2,0.4],channelPeaks:[[0.2,0.4],[0.2,0.4]]}];
      __editor.session.tracks[0].sourceId='source';
      __editor.session.tracks[0].clips=[{id:'clip',assetId:'source',start:0,offset:0,duration:10,volume:1,fadeIn:0,fadeOut:0,muted:false}];
      __editor.session.pluginChain=[{id:'gain',format:'vst3',pluginId:'gain',name:'Gain',path:'C:/VST3/Gain.vst3',bypassed:false}];
    }""")
    page.get_by_role('button', name='导出混音', exact=True).click()
    dialog = page.locator('.editor-export-modal')
    dialog.get_by_role('button', name='导出混音', exact=True).click()
    page.wait_for_function('Boolean(window.__renderTask)')
    progress = dialog.locator('.export-dialog__progress')
    expect(progress.locator('.n-spin')).to_be_visible()
    expect(progress.locator('.export-dialog__progress-copy strong')).to_have_count(0)

    def native(completed, task='__renderTask'):
        page.evaluate(f"__emitWorkerEvent({{type:'editor_plugin_render_progress',taskId:{task},payload:{{phase:'rendering',completed:{completed},total:100}}}})")

    native(20)
    expect(progress.locator('strong')).to_have_text('15%')
    native(60)
    expect(progress.locator('strong')).to_have_text('45%')
    native(30)
    expect(progress.locator('strong')).to_have_text('45%')
    page.screenshot(path=str(args.output_dir / 'native-render-45-percent.png'))
    page.evaluate('__finishRender()')
    page.wait_for_function('Boolean(window.__encodeTask)')
    expect(progress.locator('strong')).to_have_text('75%')
    native(0)
    expect(progress.locator('strong')).to_have_text('75%')
    page.evaluate("__emitWorkerEvent({type:'editor_export_progress',taskId:__encodeTask,payload:{phase:'writing',completed:40,total:100}})")
    expect(progress.locator('strong')).to_have_text('85%')
    page.evaluate("__emitWorkerEvent({type:'editor_export_progress',taskId:__encodeTask,payload:{phase:'preparing',completed:10,total:100}})")
    expect(progress.locator('strong')).to_have_text('85%')
    page.screenshot(path=str(args.output_dir / 'encoding-85-percent.png'))
    page.evaluate("__emitWorkerEvent({type:'editor_mix_exported',taskId:__encodeTask,payload:{path:'C:/Audio/mix.wav',duration:10,sampleRate:48000,channels:2,format:'wav'}})")
    expect(dialog).not_to_be_visible()
    assert not errors, errors
    report = {'preparationIsIndeterminate': True, 'nativeRenderPercentages': [15, 45, 75],
              'encodingPercentage': 85, 'lateAndRegressiveUpdatesIgnored': True,
              'pageErrors': errors}
    (args.output_dir / 'report.json').write_text(json.dumps(report, indent=2), 'utf-8')
    print(json.dumps(report, indent=2))
    context.close()
    browser.close()
