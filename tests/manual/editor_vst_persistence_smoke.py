"""Verify automatic plugin-state persistence through a real Windows Tauri app.

Uses the Fixture Gain VST3 and its public param operation; never injects state,
revision, runtime readiness or a replacement invoke bridge. It creates one new
lab project, opens the real native editor without playback, waits for frontend
autosave, reopens the project, and checks both the state container and rendered
amplitude. Launch only after the application build has completed.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import time
import traceback
from typing import Any
from urllib.request import urlopen
import uuid
import wave

from playwright.sync_api import sync_playwright
from vst_tauri_smoke import unused_port
from vst_host_smoke import read_float_wav


def wait_until(predicate: Any, description: str, timeout: float = 20) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        result = predicate()
        if result:
            return result
        if time.monotonic() >= deadline:
            raise AssertionError(description)
        time.sleep(0.1)


def fixture_gain(root: Path, token: str) -> float:
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", token), token
    data = (root / "plugin-states" / (token.split(":")[1] + ".bin")).read_bytes()
    assert hashlib.sha256(data).hexdigest() == token.split(":")[1]
    assert data[:4] == b"SCV3" and len(data) >= 20
    component_size = struct.unpack_from("<I", data, 4)[0]
    assert component_size >= 8 and len(data) >= 12 + component_size
    return struct.unpack_from("<d", data, 8)[0] * 2


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument(
        "--plugin",
        type=Path,
        required=True,
        help="Existing Fixture.vst3 bundle or binary",
    )
    parser.add_argument("--data-root", type=Path, default=root / "data-vst-lab")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("E:/pymss-vst-lab-artifacts/persistence"),
    )
    args = parser.parse_args()
    assert os.name == "nt", "This smoke test requires the Windows VST Lab app"
    app, plugin = args.app.resolve(strict=True), args.plugin.resolve(strict=True)
    data_root = args.data_root.resolve(strict=True)
    assert data_root.name == "data-vst-lab"
    run_dir = args.output_dir.resolve() / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        + "-"
        + str(os.getpid())
    )
    run_dir.mkdir(parents=True, exist_ok=False)
    scan_dir = run_dir / "plugins"
    scan_dir.mkdir()
    copied_plugin = scan_dir / "Fixture.vst3"
    if plugin.is_dir():
        shutil.copytree(plugin, copied_plugin)
    else:
        shutil.copy2(plugin, copied_plugin)
    audio = run_dir / "source.wav"
    rate, frames = 48000, 48000 * 4
    samples = [
        round(0.1 * 32767 * math.sin(2 * math.pi * 440 * index / rate))
        for index in range(frames)
    ]
    with wave.open(str(audio), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(
            b"".join(struct.pack("<hh", value, value) for value in samples)
        )
    input_peak = max(abs(value) for value in samples) / 32768
    port = unused_port()
    report: dict[str, Any] = {
        "app": str(app),
        "plugin": str(copied_plugin),
        "dataRoot": str(data_root),
        "cdpPort": port,
        "cases": [],
    }
    process, browser, page, project, playwright = None, None, None, None, None
    errors: list[str] = []
    console_errors: list[str] = []

    def invoke(command: str, arguments: dict[str, Any] | None = None) -> Any:
        assert page is not None
        result = page.evaluate(
            """async ({command,args}) => {
          let timer;
          try {return {ok:true,result:await Promise.race([window.__TAURI_INTERNALS__.invoke(command,args),
            new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('Invoke timeout')),60000)})])}}
          catch(error){return {ok:false,error:String(error)}} finally{clearTimeout(timer)}
        }""",
            {"command": command, "args": arguments or {}},
        )
        assert result["ok"], f"{command}: {result.get('error')}"
        return result["result"]

    def request(op: str, **extra: Any) -> Any:
        assert project is not None
        return invoke(
            "plugin_host_request",
            {"request": {"op": op, "projectId": project["id"], **extra}},
        )

    def case(name: str, details: Any) -> None:
        report["cases"].append({"name": name, "passed": True, "details": details})
        print("PASS " + name, flush=True)

    try:
        environment = os.environ.copy()
        environment["PYMSS_STUDIO_DATA_ROOT"] = str(data_root)
        environment["PYMSS_VST_FIXTURE_BEHAVIOR"] = ""
        environment["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
            f"--remote-debugging-port={port}"
        )
        with (run_dir / "application.log").open("w", encoding="utf-8") as log:
            playwright = sync_playwright().start()
            process = subprocess.Popen(
                [str(app)],
                cwd=root,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            report["applicationPid"] = process.pid

            def endpoint() -> Any:
                assert process.poll() is None, (
                    f"Owned app exited early ({process.returncode})"
                )
                try:
                    with urlopen(
                        f"http://127.0.0.1:{port}/json/version", timeout=0.5
                    ) as response:
                        return json.load(response)
                except Exception:
                    return None

            metadata = wait_until(
                endpoint, "WebView2 did not expose the owned CDP port"
            )
            browser = playwright.chromium.connect_over_cdp(
                metadata["webSocketDebuggerUrl"], timeout=10000
            )
            context = browser.contexts[0]
            wait_until(lambda: context.pages, "Owned WebView2 has no application page")
            page = context.pages[0]
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on(
                "console",
                lambda message: console_errors.append(message.text)
                if message.type == "error"
                else None,
            )
            page.set_default_timeout(20000)
            page.wait_for_function(
                "typeof window.__TAURI_INTERNALS__?.invoke==='function'"
            )
            page.wait_for_load_state("networkidle", timeout=30000)
            capabilities = invoke("plugin_host_capabilities")
            assert capabilities["enabled"] and capabilities["nativeEditor"]
            runtime = invoke("runtime_info", {"payload": {"backend": "cuda"}})
            assert runtime["ready"] is True, (
                "The installed lab runtime is not ready; activate it through the normal runtime workflow"
            )
            case(
                "real_lab_capability_and_runtime_probe",
                {
                    "platform": capabilities["platform"],
                    "runtimeReady": runtime["ready"],
                },
            )
            descriptors = invoke("plugin_host_scan", {"paths": [str(scan_dir)]})
            descriptor = next(
                item for item in descriptors if item["name"] == "Fixture Gain"
            )
            project = invoke(
                "create_blank_editor_project",
                {"payload": {"name": "Plugin state persistence", "locale": "en"}},
            )
            project.update(
                {
                    "version": 4,
                    "masterVolume": 1,
                    "masterPan": 0,
                    "pluginChain": [],
                    "sources": [
                        {
                            "id": "source",
                            "path": str(audio),
                            "name": audio.name,
                            "role": "reference",
                            "duration": 4,
                            "sampleRate": rate,
                            "channels": 2,
                            "missing": False,
                            "originKind": "external",
                        }
                    ],
                    "tracks": [
                        {
                            "id": "track",
                            "sourceId": "source",
                            "name": "Audio",
                            "role": "reference",
                            "volume": 1,
                            "pan": 0,
                            "muted": False,
                            "solo": False,
                            "fadeIn": 0,
                            "fadeOut": 0,
                            "pluginChain": [
                                {
                                    "id": "fixture",
                                    "format": "vst3",
                                    "pluginId": descriptor["id"],
                                    "name": descriptor["name"],
                                    "path": descriptor["path"],
                                    "bypassed": False,
                                }
                            ],
                            "clips": [
                                {
                                    "id": "clip",
                                    "assetId": "source",
                                    "start": 0,
                                    "offset": 0,
                                    "duration": 4,
                                    "volume": 1,
                                    "fadeIn": 0,
                                    "fadeOut": 0,
                                    "muted": False,
                                    "locked": False,
                                }
                            ],
                        }
                    ],
                }
            )
            invoke("save_editor_project", {"project": project})
            project_path = (
                data_root / "editor-projects" / project["id"] / "project.json"
            )
            assert project_path.is_file()
            report["projectId"], report["projectPath"] = (
                project["id"],
                str(project_path),
            )

            def persisted_token(previous: str | None = None) -> str | None:
                try:
                    persisted = json.loads(project_path.read_text(encoding="utf-8"))
                    token = persisted["tracks"][0]["pluginChain"][0].get("state")
                    return (
                        token if isinstance(token, str) and token != previous else None
                    )
                except (OSError, ValueError, KeyError, IndexError):
                    return None

            def open_native_editor() -> None:
                page.evaluate(
                    "id=>{window.location.hash='/editor?projectId='+encodeURIComponent(id)}",
                    project["id"],
                )
                page.wait_for_load_state("networkidle", timeout=30000)
                page.wait_for_function(
                    """id=>{
                  const editor=document.querySelector('#app')?.__vue_app__?.config.globalProperties.$pinia._s.get('editor');
                  return editor?.session?.id===id&&!editor.loading&&editor.session.sources.every(source=>!source.missing)
                }""",
                    arg=project["id"],
                    timeout=45000,
                )
                toggle = page.locator(".editor-shell__inspector-toggle")
                toggle.wait_for(state="visible")
                panel = page.locator(".plugin-panel")
                # Layout preferences restore asynchronously after route mounting.
                # Recheck the actual panel after that restore, instead of toggling
                # from the previous component's transient aria state.
                for _ in range(4):
                    page.wait_for_timeout(250)
                    if panel.is_visible():
                        break
                    if toggle.get_attribute("aria-pressed") == "false":
                        toggle.click()
                panel.wait_for(state="visible")
                panel.locator(".plugin-panel__slot").first.get_by_role(
                    "button", name=re.compile("打开界面|Open interface")
                ).click()
                wait_until(
                    lambda: "fixture" in request("status")["openEditors"],
                    "Real plugin editor did not open",
                )

            open_native_editor()
            initial = wait_until(
                persisted_token,
                "Opening the native editor did not automatically persist initial state",
            )
            assert request("status")["playing"] is False
            case(
                "native_editor_opened_and_initial_state_autosaved_without_playback",
                {"token": initial, "gain": fixture_gain(data_root, initial)},
            )
            params = request("parameters", slotId="fixture")["params"]
            gain = next(item for item in params if item["name"] == "Gain")
            before = request("status")
            changed = request("param", slotId="fixture", paramId=gain["id"], value=0.8)
            assert (
                changed["sessionId"] == before["sessionId"]
                and changed["stateRevision"] > before["stateRevision"]
            )
            token = wait_until(
                lambda: persisted_token(initial),
                "Native parameter edit did not update the real project JSON automatically",
            )
            assert abs(fixture_gain(data_root, token) - 0.8) < 1e-5
            page.wait_for_function("""()=>{
              const editor=document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('editor');
              return !editor.pluginStateDirty&&!editor.pluginStateError
            }""")
            assert request("status")["playing"] is False
            case(
                "real_param_edit_revision_triggers_frontend_snapshot_and_disk_save",
                {
                    "previousToken": initial,
                    "savedToken": token,
                    "gain": fixture_gain(data_root, token),
                },
            )
            page.screenshot(path=str(run_dir / "native-state-autosaved.png"))
            page.locator(".plugin-panel__slot").first.get_by_role(
                "button", name=re.compile("关闭界面|Close interface")
            ).click()
            page.evaluate("()=>{window.location.hash='/results'}")
            page.wait_for_function("!window.location.hash.startsWith('#/editor')")
            page.locator(".editor-transport").wait_for(state="detached")
            assert persisted_token() == token
            # Use the normal store load action, which calls the real load_editor_project
            # command; do not assign a project object or a state field through CDP.
            page.evaluate(
                """async id=>{
              await document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('editor').loadProject(id)
            }""",
                project["id"],
            )
            open_native_editor()
            restored = request("snapshot")["states"]["fixture"]
            assert (
                restored == token
                and abs(fixture_gain(data_root, restored) - 0.8) < 1e-5
            )
            case(
                "close_release_and_real_project_reload_restore_the_complete_plugin_state",
                {"restoredToken": restored, "reopenedFromDisk": True},
            )
            task_id = "persistence-" + uuid.uuid4().hex
            persisted = invoke("load_editor_project", {"projectId": project["id"]})
            try:
                rendered = invoke(
                    "plugin_host_render", {"project": persisted, "taskId": task_id}
                )
                rendered_copy = run_dir / "restored-render.wav"
                shutil.copy2(rendered["path"], rendered_copy)
                _, output_frames = read_float_wav(rendered_copy)
                output_peak = max(
                    abs(value) for frame in output_frames for value in frame
                )
                assert abs(output_peak / input_peak - 0.8) < 0.0005, (
                    output_peak,
                    input_peak,
                )
                case(
                    "restored_plugin_render_preserves_the_saved_gain",
                    {
                        "inputPeak": input_peak,
                        "outputPeak": output_peak,
                        "ratio": output_peak / input_peak,
                    },
                )
            finally:
                invoke("plugin_host_cleanup_render", {"taskId": task_id})
            assert not errors, errors
            assert not console_errors, console_errors
            page.locator(".window-actions button.danger").click(no_wait_after=True)
            wait_until(
                lambda: process.poll() is not None,
                "Owned app did not exit after normal close",
                timeout=15,
            )
            assert process.returncode == 0
            case(
                "normal_close_flushes_state_and_releases_the_owned_application",
                {"exitCode": process.returncode},
            )
    except Exception as error:
        report["fatalError"] = str(error)
        report["traceback"] = traceback.format_exc()
        print("FAIL " + str(error), flush=True)
        if page is not None:
            try:
                page.screenshot(path=str(run_dir / "failure.png"), timeout=5000)
            except Exception:
                pass
    finally:
        if page is not None and process is not None and process.poll() is None:
            try:
                invoke("close_current_window")
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if process is not None:
            if process.poll() is None:
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    report["forcedApplicationTermination"] = True
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=15,
                        check=False,
                    )
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            report["applicationExitCode"] = process.returncode
        if playwright is not None:
            playwright.stop()
        report["pageErrors"] = errors
        report["consoleErrors"] = console_errors
        log_text = (run_dir / "application.log").read_text(
            encoding="utf-8", errors="replace"
        )
        report["resizeObserverWarnings"] = log_text.count(
            "ResizeObserver loop completed with undelivered notifications"
        )
        report["passed"] = len(report["cases"])
        report["failed"] = int(
            "fatalError" in report or report.get("applicationExitCode", 0) != 0
        )
        (run_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(
            f"{report['passed']} passed; {report['failed']} failed; report: {run_dir / 'report.json'}",
            flush=True,
        )
    return int(report["failed"] != 0)


if __name__ == "__main__":
    sys.exit(main())
