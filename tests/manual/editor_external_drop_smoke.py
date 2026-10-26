"""Exercise native file-drop events through the real editor UI and asset store."""

import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--url", default="http://127.0.0.1:1435")
parser.add_argument("--output-dir", type=Path, default=Path(".tmp/editor-external-drop"))
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, args=["--no-proxy-server"])
    context = browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=1.5)
    page = context.new_page()
    page.set_default_timeout(15000)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script("""localStorage.setItem('pymss-studio:app-settings', JSON.stringify({
      startupOnboardingSeen:true,locale:'zh-CN',themeMode:'light'
    }))""")
    page.goto(args.url.rstrip("/") + "/#/editor", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_load_state("networkidle", timeout=60000)
    page.locator(".editor-transport").wait_for(state="visible")
    page.evaluate("""async () => {
      const app = document.querySelector('#app').__vue_app__.config.globalProperties;
      window.__editor = app.$pinia._s.get('editor');
      await app.$router.replace({name:'results'});
      const callbacks = new Map(), listeners = new Map();
      let nextId = 0;
      window.__saved = [];
      window.__imports = [];
      window.__TAURI_INTERNALS__ = {
        metadata:{currentWindow:{label:'main'},currentWebview:{label:'main'}},
        transformCallback:callback=>{const id=++nextId;callbacks.set(id,callback);return id},
        unregisterCallback:id=>callbacks.delete(id),
        convertFileSrc:path=>path,
        invoke:async(command,args)=>{
          if(command==='plugin:event|listen') {
            listeners.set(args.handler,{event:args.event,callback:callbacks.get(args.handler)});
            return args.handler;
          }
          if(command==='plugin:event|unlisten') {listeners.delete(args.eventId);return}
          if(command==='import_editor_assets') {
            __imports.push(structuredClone(args));
            if(window.__importGate) await window.__importGate;
            if(window.__failImport) {window.__failImport=false;throw new Error('Import denied')}
            return {files:[...new Set(args.paths)].filter(path=>/\\.(wav|flac|mp3)$/i.test(path))
              .map(path=>({path,name:path.split('/').pop(),originKind:'external'})),warnings:[]};
          }
          if(command==='get_audio_metadata') {
            if(args.payload.path.endsWith('/broken.wav')) throw new Error('Invalid audio data');
            return {path:args.payload.path,name:args.payload.path.split('/').pop(),duration:12,sampleRate:48000,channels:2};
          }
          if(command==='generate_waveform_peaks') return {
            path:args.payload.path,peaksPath:null,peaks:[0.2,0.6,0.3],duration:12,sampleRate:48000,channels:2
          };
          if(command==='save_editor_project') {__saved.push(structuredClone(args.project));return args.project}
          if(command==='load_app_store') return null;
          if(command==='save_app_store'||command==='write_frontend_log') return;
          throw new Error('Unexpected command: '+command);
        }
      };
      window.__emitDrop = (type,point,paths=[])=>{
        const event='tauri://drag-'+type;
        const payload={position:{x:point.x*devicePixelRatio,y:point.y*devicePixelRatio},paths};
        for(const {event:name,callback} of listeners.values()) {
          if(name===event) callback({event,id:1,payload});
        }
      };
      __editor.session={id:'drop-project',name:'Mix',masterVolume:1,masterPan:0,
        sources:[],tracks:[],createdAt:1,updatedAt:1};
      __editor.clearHistory();
      await app.$router.replace({name:'editor',query:{projectId:'drop-project'}});
    }""")
    page.locator(".editor-mixer").wait_for(state="visible")

    def point(locator):
        box = locator.bounding_box()
        assert box is not None
        return {"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2}

    def drop_event(event_type, target, paths=None):
        page.evaluate("args=>__emitDrop(args.type,args.point,args.paths)", {
            "type": event_type, "point": target, "paths": paths or [],
        })

    mixer = page.locator(".editor-mixer")
    expect(page.locator(".editor-assets")).to_contain_text("拖到此处加入资产库")
    target = point(mixer)
    drop_event("over", target)
    expect(mixer).to_have_class("editor-mixer editor-mixer--external-drop")
    expect(page.locator(".external-drop-hint")).to_contain_text("新增参考音轨")
    page.screenshot(path=str(args.output_dir / "drop-preview.png"))
    drop_event("drop", target, ["/audio/first.wav", "/audio/second.flac"])
    page.wait_for_function("__editor.session.tracks.length===2")
    assert page.evaluate("__editor.session.sources.length") == 2
    assert page.evaluate("__editor.session.tracks.every(t=>t.clips.length===1&&t.clips[0].start===0)")
    page.evaluate("__editor.undo()")
    page.wait_for_function("__editor.session.tracks.length===0")
    assert page.evaluate("__editor.session.sources.length") == 2
    assert not page.evaluate("__editor.canUndo")
    page.evaluate("__editor.redo()")
    assert page.evaluate("__editor.session.tracks.length") == 2
    print("Empty timeline, batch import and undo/redo verified", flush=True)

    # Reusing an imported file creates a new track without another asset entry.
    expect(page.locator(".external-drop-hint")).to_have_count(0)
    drop_event("drop", target, ["/audio/first.wav"])
    page.wait_for_function("__editor.session.tracks.length===3")
    assert page.evaluate("__editor.session.sources.length") == 2
    asset_target = point(page.locator(".editor-shell__asset-panel"))
    drop_event("over", asset_target)
    expect(mixer).not_to_have_class("editor-mixer editor-mixer--external-drop")
    drop_event("drop", asset_target, ["/audio/library-only.mp3"])
    page.wait_for_function("__editor.session.sources.length===3")
    expect(page.locator(".external-drop-hint")).to_have_count(0)
    assert page.evaluate("__editor.session.tracks.length") == 3
    print("Asset reuse and library-only drop verified", flush=True)

    # Collapsing the library must not remove the direct drop target.
    page.locator(".editor-shell__asset-toggle").click()
    page.wait_for_timeout(250)
    target = point(mixer)
    drop_event("over", target)
    expect(page.locator(".external-drop-hint")).to_be_visible()
    drop_event("leave", target)
    expect(page.locator(".external-drop-hint")).to_have_count(0)
    count = page.evaluate("__imports.length")
    drop_event("drop", {"x": 2, "y": 2}, ["/audio/outside.wav"])
    assert page.evaluate("__imports.length") == count

    page.evaluate("() => {window.__importGate=new Promise(resolve=>window.__releaseImport=resolve)}")
    drop_event("drop", target, ["/audio/fourth.wav"])
    expect(page.locator(".external-drop-hint")).to_contain_text("正在导入")
    record_button = page.locator(".transport-record")
    expect(record_button).to_be_disabled()
    expect(record_button).to_have_attribute("title", "正在导入音频，请稍后开始录音")
    count = page.evaluate("__imports.length")
    drop_event("drop", target, ["/audio/ignored.wav"])
    assert page.evaluate("__imports.length") == count
    page.evaluate("__releaseImport();window.__importGate=null")
    page.wait_for_function("__editor.session.tracks.length===4")
    expect(page.locator(".external-drop-hint")).to_have_count(0)
    expect(record_button).to_be_enabled()
    drop_event("drop", target, ["/audio/broken.wav"])
    expect(page.get_by_text("文件导入未完成：无法导入“broken.wav”：Invalid audio data", exact=True)).to_be_visible()
    expect(page.locator(".external-drop-hint")).to_have_count(0)
    assert page.evaluate("__editor.session.tracks.length") == 4
    assert page.evaluate("__editor.session.sources.length") == 4
    print("Recording start guard and metadata failure handling verified", flush=True)
    drop_event("drop", target, ["/audio/readme.txt"])
    expect(page.get_by_text("未发现可用音频文件", exact=True)).to_be_visible()
    expect(page.locator(".external-drop-hint")).to_have_count(0)
    page.evaluate("window.__failImport=true")
    drop_event("drop", target, ["/audio/denied.wav"])
    expect(page.get_by_text("Import denied", exact=True)).to_be_visible()
    expect(page.locator(".external-drop-hint")).to_have_count(0)
    assert page.evaluate("__editor.session.tracks.length") == 4
    page.evaluate("__editor.saveProject()")
    assert page.evaluate("__saved.at(-1).tracks.length") == 4
    assert page.evaluate("__saved.at(-1).sources.length") == 4
    page.wait_for_function("!document.querySelector('.n-message')")
    page.screenshot(path=str(args.output_dir / "imported-tracks.png"))
    assert not errors, errors
    browser.close()
    print(json.dumps({"directDrop": "passed", "batchUndo": "passed", "assetReuse": "passed",
                      "collapsedLibrary": "passed", "progressAndRetry": "passed", "save": "passed"}))
