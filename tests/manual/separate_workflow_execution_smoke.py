"""Exercise both workflow entry points with browser stores and the real Worker."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf
from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument("--url", default="http://127.0.0.1:1420")
parser.add_argument("--runtime", type=Path)
args = parser.parse_args()
root = Path(__file__).resolve().parents[2]

simple = {
    "version": 1, "defaults": {"device": "cpu", "output_format": "wav"}, "steps": [],
    "ensembles": [{"id": "phase", "algorithm": "invert", "output_stem": "Inverted",
                   "save": "Default", "inputs": [{"source": "input", "weight": 1}]}],
}
graph = {
    "nodes": [
        {"id": 1, "type": "pymss_load_audio", "pos": [0, 0], "size": [240, 120],
         "widgets_values": ["input.wav", "input"],
         "outputs": [{"name": "audio", "type": "AUDIO", "links": [1]}]},
        {"id": 2, "type": "pymss_save_audio", "pos": [300, 0], "size": [240, 240],
         "widgets_values": ["wav", "Default", "44100", "FLOAT", "PCM_24", "320k"],
         "inputs": [{"name": "audio", "type": "AUDIO", "link": 1}]},
    ],
    "links": [[1, 1, 0, 2, 0, "AUDIO"]], "version": 0.4,
    "last_node_id": 2, "last_link_id": 1,
}


def entry(identifier, name, definition, format):
    return {"id": identifier, "name": name, "description": "", "definition": definition,
            "format": format, "formatVersion": 1, "createdAt": 1, "updatedAt": 1}


multi = json.loads(json.dumps(graph))
multi["nodes"].extend([
    {"id": 3, "type": "pymss_load_audio", "pos": [0, 300], "size": [240, 120],
     "widgets_values": ["second.wav", ""], "outputs": [{"name": "audio", "type": "AUDIO", "links": [2]}]},
    {"id": 4, "type": "pymss_save_audio", "pos": [300, 300], "size": [240, 240],
     "widgets_values": ["wav", "Default", "44100", "FLOAT", "PCM_24", "320k"],
     "inputs": [{"name": "audio", "type": "AUDIO", "link": 2}]},
])
multi["links"].append([2, 3, 0, 4, 0, "AUDIO"])
multi.update(last_node_id=4, last_link_id=2)
state = {"selectedWorkflowId": "multi", "workflows": [
    entry("simple", "Phase inversion", simple, "simple"),
    entry("graph", "Audio passthrough", graph, "graph"),
    entry("multi", "Two sources", multi, "graph"),
    entry("no-input", "No source", {"nodes": [graph["nodes"][1]], "links": []}, "graph"),
    entry("unfinished", "Unfinished", {"version": 1, "steps": []}, "simple"),
]}

with tempfile.TemporaryDirectory(prefix="pymss-separate-workflow-") as temporary, sync_playwright() as playwright:
    directory = Path(temporary)
    sources = [directory / "first.wav", directory / "second.wav"]
    for source in sources:
        sf.write(source, np.full((4410, 2), 0.2, dtype=np.float32), 44100, subtype="FLOAT")
    multi["nodes"][0]["widgets_values"] = [str(sources[0]), ""]
    multi["nodes"][2]["widgets_values"] = [str(sources[1]), ""]
    browser = playwright.chromium.launch(headless=True, args=["--no-proxy-server"])
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    page.set_default_timeout(10000)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script(
        "localStorage.setItem('pymss-studio:app-settings', JSON.stringify(" + json.dumps({
            "startupOnboardingSeen": True, "locale": "en", "themeMode": "light",
            "outputDir": str(directory / "outputs"), "clearInputAfterSubmit": False,
        }) + ")); localStorage.setItem('pymss-studio:workflow-state', JSON.stringify(" + json.dumps(state) + "));"
    )
    page.goto(args.url.rstrip("/") + "/#/?mode=workflow")
    page.wait_for_load_state("networkidle")
    expect(page.locator(".target-pane")).to_contain_text("Phase inversion")
    assert page.url.endswith("/#/?mode=workflow"), page.url

    def install_transport():
        page.evaluate("""() => {
          window.__stores = document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s;
          window.__requests = [];
          window.__TAURI_EVENT_PLUGIN_INTERNALS__ = {unregisterListener() {}};
          window.__TAURI_INTERNALS__ = {
            metadata: {currentWindow: {label: 'main'}, currentWebview: {label: 'main'}}, transformCallback: () => 1,
            invoke: async (command, args) => {
              if (command === 'start_workflow_inference') {
                window.__requests.push(JSON.parse(JSON.stringify(args.payload)));
                return {taskId: args.payload.taskId, started: true};
              }
              if (command === 'save_app_store') {localStorage.setItem('pymss-studio:' + args.name, JSON.stringify(args.data)); return;}
              if (command === 'load_app_store') return JSON.parse(localStorage.getItem('pymss-studio:' + args.name) || 'null');
              if (command === 'mutate_workflow_store') {
                const state = JSON.parse(localStorage.getItem('pymss-studio:workflow-state'));
                const entry = args.payload.entry;
                const index = state.workflows.findIndex(item => item.id === entry.id);
                if (index < 0) state.workflows.push(entry); else state.workflows[index] = entry;
                state.selectedWorkflowId = entry.id;
                localStorage.setItem('pymss-studio:workflow-state', JSON.stringify(state));
                return {state};
              }
              if (command === 'collect_audio_inputs') return window.__inputFiles || [];
              return null;
            },
          };
        }""")

    install_transport()

    drawer = page.locator(".settings-drawer__content")
    for format in ["wav", "flac", "mp3", "m4a"]:
        page.evaluate("format => window.__stores.get('settings').defaultFormat = format", format)
        page.get_by_role("button", name="Advanced Settings", exact=True).click()
        expect(drawer).to_be_visible()
        expect(drawer.get_by_role("checkbox")).to_have_count(0)
        expect(drawer.locator(".n-collapse")).to_have_count(0)
        expect(drawer.locator(".n-select")).to_have_count(2 if format == "m4a" else 0)
        if format != "m4a":
            expect(drawer).to_contain_text("follow the workflow's save node settings")
        page.keyboard.press("Escape")
        expect(drawer).to_be_hidden()
    page.evaluate("window.__stores.get('settings').defaultFormat = 'wav'")
    page.locator(".mode-switch").get_by_text("Single Model", exact=True).click()
    page.get_by_role("button", name="Advanced Settings", exact=True).click()
    expect(drawer.get_by_role("checkbox", name="Enable TTA", exact=True)).to_be_visible()
    expect(drawer.locator(".n-collapse")).to_be_visible()
    expect(drawer.locator(".n-select")).to_have_count(1)
    page.keyboard.press("Escape")
    expect(drawer).to_be_hidden()
    page.locator(".mode-switch").get_by_text("Workflow", exact=True).click()

    def execute_request(index, expected_value, outputs_per_task=1):
        page.wait_for_function("index => window.__requests.length > index", arg=index)
        payload = page.evaluate("index => window.__requests[index]", index)
        request = directory / "request.json"
        request.write_text(json.dumps(payload), encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, str(root / "python/worker.py"), "infer_workflow", "--payload", str(request)],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", **(
                {"PYTHONPATH": str(args.runtime.resolve())} if args.runtime else {}
            )},
        )
        assert completed.returncode == 0, (completed.stdout + completed.stderr)[-6000:]
        events = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{")]
        outputs = [output for event in events if event.get("type") == "task_done" for output in event["payload"]["outputs"]]
        assert len(outputs) == outputs_per_task * len(payload.get("tasks", [payload])), completed.stdout[-3000:]
        for output in outputs:
            audio, rate = sf.read(output["path"], always_2d=True)
            assert rate == 44100 and audio.shape == (4410, 2)
            np.testing.assert_allclose(audio, expected_value, atol=1e-6)
        for event in events:
            page.evaluate("event => window.__stores.get('task').handleWorkerEvent(event)", event)
        return payload

    search = page.locator(".target-toolbar--single input")
    search.fill("passthrough")
    expect(page.locator(".target-pane [role=option]")).to_have_count(1)
    search.fill("")
    expect(page.locator(".stage-head")).to_contain_text("Selected files are processed individually")
    multi_row = page.locator(".workflow-target").filter(has_text="Two sources")
    expect(multi_row).to_contain_text("Workflows only")
    expect(multi_row).to_contain_text("2 input nodes")
    expect(multi_row).to_have_attribute("aria-disabled", "true")
    page.evaluate("files => window.__stores.get('task').inputFiles = files", [str(source) for source in sources])
    expect(page.locator(".launch-bar__go")).to_be_disabled()
    expect(page.locator(".launch-bar__status")).to_contain_text("2 input nodes")
    page.locator(".target-pane [role=option]").filter(has_text="Phase inversion").click()
    multi_row.click(force=True)
    assert page.evaluate("window.__stores.get('workflow').selectedWorkflowId") == "simple"
    no_input_row = page.locator(".workflow-target").filter(has_text="No source")
    expect(no_input_row).to_contain_text("No input node")
    expect(no_input_row).to_have_attribute("aria-disabled", "true")
    unfinished = page.locator(".target-pane [role=option]").filter(has_text="Unfinished")
    expect(unfinished).to_have_attribute("aria-disabled", "true")
    unfinished.click(force=True)
    assert page.evaluate("window.__stores.get('workflow').selectedWorkflowId") == "simple"
    artifacts = root / ".tmp" / "separate-workflow-ui"
    artifacts.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(artifacts / "workflow-selection.png"), full_page=True)
    heights = page.locator(".workflow-target").evaluate_all("nodes => nodes.map(node => node.getBoundingClientRect().height)")
    assert max(heights) - min(heights) <= 1, heights
    page.evaluate("window.__stores.get('settings').locale = 'zh-CN'")
    page.set_viewport_size({"width": 1280, "height": 850})
    expect(multi_row).to_contain_text("仅工作流页")
    page.screenshot(path=str(artifacts / "workflow-selection-zh.png"), full_page=True)
    page.evaluate("window.__stores.get('settings').locale = 'en'")
    expect(multi_row).to_contain_text("Workflows only")
    page.set_viewport_size({"width": 1100, "height": 760})
    assert page.locator(".app-content").evaluate("node => node.scrollWidth <= node.clientWidth + 1")
    expect(page.get_by_role("button", name="Open Workflows Page", exact=True)).to_have_count(0)
    page.screenshot(path=str(artifacts / "workflow-selection-compact.png"), full_page=True)
    page.set_viewport_size({"width": 1440, "height": 1000})
    for identifier, name, expected in [("simple", "Phase inversion", -0.2), ("graph", "Audio passthrough", 0.2)]:
        page.locator(".target-pane [role=option]").filter(has_text=name).click()
        page.evaluate("""files => {
          const task = window.__stores.get('task'); task.inputFiles = files;
          task.ensembleEnabled = true; task.ensembleModels = [];
        }""", [str(source) for source in sources])
        first = page.evaluate("window.__requests.length")
        expect(page.locator(".launch-bar__go")).to_be_enabled()
        page.locator(".launch-bar__go").click()
        payload = execute_request(first, expected)
        assert len(payload["tasks"]) == 2
        assert not payload.get("workbench")
        configs = page.evaluate("id => window.__stores.get('task').tasks.filter(task => task.runConfig.workflowId === id).map(task => task.runConfig)", identifier)
        assert len(configs) == 2 and all(not config.get("workbench") for config in configs)
        page.locator(".stage-actions").get_by_role("button", name="New Separation", exact=True).click()
        expect(page.locator(".target-pane")).to_be_visible()

    page.locator(".mode-switch").get_by_text("Single Model", exact=True).click()
    expect(page.locator(".target-pane .stage-empty")).to_be_visible()
    page.locator(".mode-switch").get_by_text("Workflow", exact=True).click()
    expect(page.locator(".target-pane")).to_contain_text("Audio passthrough")
    page.locator(".console-topbar__manage").click()
    expect(page).to_have_url(args.url.rstrip("/") + "/#/workflows")
    assert page.evaluate("window.__stores.get('workflow').selectedWorkflowId") == "graph"
    page.evaluate("delete window.__TAURI_INTERNALS__")
    page.goto(args.url.rstrip("/") + "/#/workflow-simple-editor?workflowId=simple")
    page.wait_for_load_state("networkidle")
    page.add_style_tag(content=".title-bar { display: none !important; }")
    expect(page.locator("[data-workbench-run-panel]")).to_be_visible()
    install_transport()
    page.evaluate("""files => {
      window.__inputFiles = files;
      const session = window.__stores.get('workbench').session('simple');
      session.bindings.inputs.input = {files};
      session.bindings.primaryInput = 'input';
    }""", [str(sources[0])])
    page.locator("[data-workbench-run-panel]").get_by_role("button", name="Run", exact=True).click()
    page.wait_for_function("window.__stores.get('task').tasks.some(task => task.runConfig?.workbench)")
    payload = execute_request(0, -0.2)
    assert payload.get("workbench")

    page.evaluate("delete window.__TAURI_INTERNALS__")
    page.goto(args.url.rstrip("/") + "/#/workflow-node-editor?workflowId=multi")
    page.wait_for_load_state("networkidle")
    page.add_style_tag(content=".title-bar { display: none !important; }")
    expect(page.locator("[data-workbench-run-panel]")).to_be_visible()
    install_transport()
    expect(page.locator("[data-workbench-run-panel]").get_by_role("button", name="Run", exact=True)).to_be_enabled()
    page.locator("[data-workbench-run-panel]").get_by_role("button", name="Run", exact=True).click()
    payload = execute_request(0, 0.2, outputs_per_task=2)
    assert not payload.get("input") and not payload.get("workbench")
    assert page.evaluate("window.__stores.get('task').tasks.some(task => task.runConfig?.workflowId === 'multi' && task.runConfig.workbench?.nativeGraph)")
    assert payload["workflow"]["nodes"][0]["widgets_values"][0] == str(sources[0])
    assert payload["workflow"]["nodes"][2]["widgets_values"][0] == str(sources[1])
    assert not errors, errors
    print(json.dumps({"separationWorkflowTasks": 4, "formats": ["simple", "graph"],
                      "workbenchTasks": 2, "multiInputBlockedOnSeparate": True}))
    browser.close()
