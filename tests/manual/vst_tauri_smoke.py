"""Verify the Windows VST Lab through its real Tauri/WebView2 invoke bridge.

Requires Python Playwright. The script starts its own application process with
lab-only data, chooses an unused CDP port, creates one new editor project, and
terminates only that process and its descendants. It never installs plugins or
changes application settings. Device playback uses master volume zero.

Example::

    python tests/manual/vst_tauri_smoke.py --app APP_EXE --plugin NOVA_VST3 \
        --input AUDIO_WAV --data-root LAB_DATA --output-dir ARTIFACTS
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from typing import Any
from urllib.request import urlopen
import uuid
import wave

from playwright.sync_api import sync_playwright


def unused_port() -> int:
    for port in (9223, 9224, 9225, 9226):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("CDP ports 9223 through 9226 are already occupied")


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--plugin", type=Path, required=True, help="An existing TDR Nova VST3 bundle")
    parser.add_argument("--input", type=Path, required=True, help="A short local PCM WAV")
    parser.add_argument("--data-root", type=Path, default=root / "data-vst-lab")
    parser.add_argument("--output-dir", type=Path, default=root / ".tmp/vst-tauri-smoke")
    parser.add_argument("--runtime-python", type=Path, help="Activate an existing interpreter below lab runtime-envs through the official command")
    parser.add_argument("--skip-screenshot", action="store_true")
    args = parser.parse_args()
    assert os.name == "nt", "This first-round Tauri smoke test requires Windows"
    app = args.app.resolve(strict=True)
    plugin = args.plugin.resolve(strict=True)
    audio = args.input.resolve(strict=True)
    data_root = args.data_root.resolve()
    assert data_root.name == "data-vst-lab", "Use the isolated data-vst-lab data directory"
    runtime_python = args.runtime_python.resolve(strict=True) if args.runtime_python else None
    if runtime_python is not None:
        assert runtime_python.parent.name == "Scripts" and runtime_python.parents[2].name == "runtime-envs"
        runtime_backend = runtime_python.parents[1].name
    port = unused_port()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir = args.output_dir.resolve() / f"{stamp}-{os.getpid()}"
    run_dir.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {"app": str(app), "plugin": str(plugin), "dataRoot": str(data_root),
                              "cdpPort": port, "cases": []}
    process: subprocess.Popen[Any] | None = None
    browser = None
    page = None
    project: dict[str, Any] | None = None
    console_errors: list[str] = []

    def invoke(command: str, arguments: dict[str, Any] | None = None, *, expect_error: bool = False) -> Any:
        assert page is not None
        response = page.evaluate("""async ({command, args}) => {
            let timer;
            try {
                const result = await Promise.race([
                    window.__TAURI_INTERNALS__.invoke(command, args),
                    new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('Invoke timeout')), 60000); })
                ]);
                return {ok: true, result};
            } catch (error) { return {ok: false, error: String(error)}; }
            finally { clearTimeout(timer); }
        }""", {"command": command, "args": arguments or {}})
        if expect_error:
            assert not response["ok"], f"Expected {command} to fail"
            return response["error"]
        assert response["ok"], f"{command}: {response.get('error')}"
        return response["result"]

    def request(operation: str, **arguments: Any) -> Any:
        assert project is not None
        return invoke("plugin_host_request", {"request": {"op": operation, "projectId": project["id"], **arguments}})

    def case(name: str, function: Any) -> None:
        started = time.monotonic()
        details = function()
        report["cases"].append({"name": name, "status": "passed", "seconds": round(time.monotonic() - started, 3),
                                "details": details})
        print(f"PASS {name}", flush=True)

    try:
        environment = os.environ.copy()
        environment["PYMSS_STUDIO_DATA_ROOT"] = str(data_root)
        environment["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = f"--remote-debugging-port={port}"
        with (run_dir / "application.log").open("w", encoding="utf-8") as log, sync_playwright() as playwright:
            process = subprocess.Popen([str(app)], cwd=root, env=environment, stdout=log, stderr=subprocess.STDOUT)
            report["applicationPid"] = process.pid
            endpoint = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 20
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f"The test application exited before CDP became ready ({process.returncode})")
                try:
                    with urlopen(endpoint + "/json/version", timeout=0.4) as response:
                        metadata = json.load(response)
                    break
                except Exception:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("The owned WebView2 process did not expose CDP within 20 seconds") from None
                    time.sleep(0.1)
            # WebView2 rejects the /json/version/ spelling used by Playwright's
            # HTTP discovery. Its advertised WebSocket endpoint works directly.
            browser = playwright.chromium.connect_over_cdp(metadata["webSocketDebuggerUrl"], timeout=10000)
            assert browser.contexts, "CDP has no WebView2 browser context"
            context = browser.contexts[0]
            deadline = time.monotonic() + 10
            while not context.pages:
                if time.monotonic() >= deadline:
                    raise RuntimeError("CDP has no application page")
                time.sleep(0.1)
            page = context.pages[0]
            page.on("pageerror", lambda error: console_errors.append(str(error)))
            page.wait_for_function("window.__TAURI_INTERNALS__ && typeof window.__TAURI_INTERNALS__.invoke === 'function'", timeout=20000)
            page.wait_for_load_state("networkidle", timeout=20000)
            report["pageUrl"] = page.url

            def capabilities_and_updates() -> Any:
                capabilities = invoke("plugin_host_capabilities")
                assert capabilities["enabled"] and capabilities["nativeEditor"] and capabilities["formats"] == ["vst3"]
                info = invoke("get_build_info")
                assert info["updateSupported"] is False
                for command, extra in (("check_managed_update", {}), ("start_managed_update", {"expectedVersion": "0.0.0"})):
                    error = invoke(command, {"channel": "stable", **extra}, expect_error=True)
                    assert "disabled in vst lab" in error.lower(), error
                return capabilities

            case("lab_capabilities_and_disabled_updater", capabilities_and_updates)

            if runtime_python is not None:
                def activate_runtime() -> Any:
                    before = invoke("runtime_info", {"payload": {"backend": runtime_backend}})
                    managed_root = Path(before["runtimeEnvsDir"]).resolve()
                    assert runtime_python.parents[2] == managed_root, (
                        f"The interpreter must be installed inside the application's managed runtime root: {managed_root}")
                    activated = invoke("activate_runtime", {"payload": {"backend": runtime_backend, "pythonPath": str(runtime_python)}})
                    assert activated["backend"] == runtime_backend
                    info = invoke("runtime_info", {"payload": {"backend": runtime_backend}})
                    assert info["ready"] is True, "The normally activated runtime failed its readiness probe"
                    return {"backend": info["backend"], "pymssVersion": info["pymssVersion"],
                            "pymssCoreVersion": info["pymssCoreVersion"], "torchBackend": info["torchBackend"],
                            "runtimeEnvsDir": info["runtimeEnvsDir"], "ready": info["ready"]}

                case("official_runtime_activation_and_probe", activate_runtime)
            descriptors = invoke("plugin_host_scan", {"paths": [str(plugin.parent)]})
            descriptor = next(item for item in descriptors if item["name"] == "TDR Nova")
            assert not descriptor["isInstrument"]
            case("single_bundle_scan", lambda: {"count": len(descriptors), "name": descriptor["name"]})
            project = invoke("create_blank_editor_project", {"payload": {"name": "Plugin compatibility", "locale": "en"}})
            report["projectId"] = project["id"]
            with wave.open(str(audio), "rb") as stream:
                sample_rate, channels, count = stream.getframerate(), stream.getnchannels(), stream.getnframes()
            duration = count / sample_rate
            project.update({"version": 4, "masterVolume": 0, "masterPan": 0, "pluginChain": [],
                            "sources": [{"id": "source", "path": str(audio), "name": audio.name, "role": "reference",
                                         "duration": duration, "sampleRate": sample_rate, "channels": channels,
                                         "missing": False, "originKind": "external"}],
                            "tracks": [{"id": "track", "sourceId": "source", "name": "Audio", "role": "reference",
                                        "volume": 1, "pan": 0, "muted": False, "solo": False, "fadeIn": 0, "fadeOut": 0,
                                        "pluginChain": [{"id": "nova", "format": "vst3", "pluginId": descriptor["id"],
                                                         "name": descriptor["name"], "path": descriptor["path"], "bypassed": False}],
                                        "clips": [{"id": "clip", "assetId": "source", "start": 0, "offset": 0,
                                                   "duration": duration, "volume": 1, "fadeIn": 0, "fadeOut": 0,
                                                   "muted": False, "locked": False}]}]})
            saved = invoke("save_editor_project", {"project": project})
            loaded = invoke("load_editor_project", {"projectId": project["id"]})
            assert saved["id"] == loaded["id"] == project["id"] and loaded["tracks"][0]["pluginChain"][0]["pluginId"] == descriptor["id"]
            case("new_project_save_and_load", lambda: {"projectId": project["id"], "version": loaded["version"]})

            def transport_and_editor() -> Any:
                request("load", project=project)
                request("play", position=0, loop=True)
                deadline = time.monotonic() + 10
                while True:
                    status = request("status")
                    assert not status.get("error"), status
                    if status["position"] > min(0.1, duration / 3):
                        break
                    assert time.monotonic() < deadline, "Playback did not advance"
                    time.sleep(0.02)
                request("pause")
                status = request("seek", position=duration / 2)
                assert not status["playing"] and abs(status["position"] - duration / 2) <= 1 / status["sampleRate"]
                assert request("editor", slotId="nova", open=True)["open"]
                assert not request("editor", slotId="nova", open=False)["open"]
                return {"sampleRate": status["sampleRate"], "position": status["position"]}

            case("real_nova_transport_and_native_editor", transport_and_editor)

            def state_reference_save_restore() -> Any:
                snapshot = request("snapshot")
                token = snapshot["states"]["nova"]
                assert re.fullmatch(r"sha256:[0-9a-f]{64}", token), "Snapshot returned inline state instead of a reference"
                path = data_root / "plugin-states" / (token.split(":", 1)[1] + ".bin")
                state = path.read_bytes()
                assert hashlib.sha256(state).hexdigest() == token.split(":", 1)[1]
                project["tracks"][0]["pluginChain"][0]["state"] = token
                invoke("save_editor_project", {"project": project})
                persisted = invoke("load_editor_project", {"projectId": project["id"]})
                assert persisted["tracks"][0]["pluginChain"][0]["state"] == token
                request("unload")
                request("load", project=persisted)
                return {"reference": token, "stateBytes": len(state)}

            case("content_addressed_plugin_state_save_and_restore", state_reference_save_restore)

            def project_isolation() -> Any:
                error = invoke("plugin_host_request", {"request": {"op": "status", "projectId": "stale-smoke-project"}}, expect_error=True)
                assert "another project" in error.lower() or "closed" in error.lower(), error
                status = request("status")
                assert status["projectId"] == project["id"]
                return {"rejected": True}

            case("tauri_project_session_isolation", project_isolation)

            def render_and_cleanup() -> Any:
                task_id = "tauri-smoke-" + uuid.uuid4().hex
                rendered = invoke("plugin_host_render", {"project": project, "taskId": task_id})
                path = Path(rendered["path"]).resolve(strict=True)
                expected = (data_root / "temp/vst-renders" / task_id / "mix.wav").resolve()
                assert path == expected and path.stat().st_size > 100
                report["nativeRenderDuration"] = rendered["duration"]
                invoke("plugin_host_cleanup_render", {"taskId": task_id})
                assert not path.parent.exists(), "Rendered temporary directory survived cleanup"
                return {"sampleRate": rendered["sampleRate"], "duration": rendered["duration"], "tailLimited": rendered["tailLimited"]}

            case("tauri_isolated_render_and_cleanup", render_and_cleanup)

            def render_cancellation() -> Any:
                task_id = "tauri-cancel-" + uuid.uuid4().hex
                long_project = copy.deepcopy(project)
                template = long_project["tracks"][0]["clips"][0]
                long_project["tracks"][0]["clips"] = [{**template, "id": f"clip-{index}", "start": index * duration}
                                                        for index in range(30)]
                page.evaluate("""({project, taskId}) => {
                    window.__vstSmokeRender = window.__TAURI_INTERNALS__.invoke('plugin_host_render', {project, taskId})
                        .then(result => ({ok: true, result}), error => ({ok: false, error: String(error)}));
                }""", {"project": long_project, "taskId": task_id})
                deadline = time.monotonic() + 5
                while not invoke("plugin_host_cancel_render", {"taskId": task_id}):
                    assert time.monotonic() < deadline, "Render was never registered for cancellation"
                    time.sleep(0.01)
                result = page.evaluate("async () => await window.__vstSmokeRender")
                assert not result["ok"], "Cancelled rendering unexpectedly completed"
                invoke("plugin_host_cleanup_render", {"taskId": task_id})
                assert not (data_root / "temp/vst-renders" / task_id).exists()
                assert request("status")["projectId"] == project["id"], "Cancelling export affected the preview session"
                return {"cancelled": True, "temporaryFilesRemoved": True}

            case("tauri_cancel_render_and_keep_preview_session", render_cancellation)
            request("unload")

            if runtime_python is not None or not args.skip_screenshot:
                try:
                    page.evaluate("id => { window.location.hash = '/editor?projectId=' + encodeURIComponent(id); }", project["id"])
                    page.wait_for_load_state("networkidle", timeout=10000)
                    page.wait_for_function("""id => {
                        const pinia = document.querySelector('#app')?.__vue_app__?.config.globalProperties.$pinia;
                        const editor = pinia?._s.get('editor');
                        return editor?.session?.id === id && !editor.loading;
                    }""", arg=project["id"], timeout=45000)
                    report["editorUiState"] = page.evaluate("""() => {
                        const editor = document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('editor');
                        return {projectId: editor.session.id, missingSources: editor.session.sources.filter(source => source.missing).length};
                    }""")
                    if report["editorUiState"]["missingSources"]:
                        report["screenshotWarning"] = (
                            "The editor marked an input offline during metadata hydration. "
                            "The isolated lab may lack an active Python runtime; inspect application.log. "
                            "The native invoke tests above use supplied metadata and do not prove this UI playback path.")
                    if runtime_python is not None:
                        assert report["editorUiState"]["missingSources"] == 0, "The active runtime did not resolve editor source metadata"
                        page.wait_for_function("""() => {
                            const editor = document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('editor');
                            return editor.session.sources.every(source => source.peaks?.length && source.peaks[0] !== -1);
                        }""", timeout=45000)
                        case("real_editor_metadata_and_waveform_peaks", lambda: report["editorUiState"])

                        def page_transport_and_save() -> Any:
                            play = page.locator(".transport-play")
                            play.click()
                            page.wait_for_function("document.querySelector('.transport-play')?.dataset.state === 'pause'", timeout=10000)
                            deadline = time.monotonic() + 10
                            while True:
                                status = request("status")
                                if status["playing"] and status["position"] > min(0.1, duration / 3):
                                    break
                                assert time.monotonic() < deadline, "The real editor play button did not advance native playback"
                                time.sleep(0.02)
                            play.click()
                            page.wait_for_function("document.querySelector('.transport-play')?.dataset.state === 'play'", timeout=10000)
                            page.locator(".transport-actions__buttons button").first.click()
                            page.wait_for_function("""() => {
                                const editor = document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('editor');
                                return !editor.saving;
                            }""", timeout=10000)
                            persisted = invoke("load_editor_project", {"projectId": project["id"]})
                            assert not persisted["sources"][0].get("missing")
                            assert persisted["tracks"][0]["pluginChain"][0]["state"].startswith("sha256:")
                            return {"nativePosition": status["position"], "sourceOnline": True, "stateReferenceSaved": True}

                        case("real_editor_play_pause_and_save_buttons", page_transport_and_save)

                        def encoded_exports() -> Any:
                            def probe_audio(path: Path) -> Any:
                                probe = subprocess.run([str(runtime_python), "-X", "utf8", "-c",
                                    "import json,sys,numpy as np,soundfile as sf; p=sys.argv[1]; info=sf.info(p); audio,_=sf.read(p); "
                                    "print(json.dumps({'format':info.format,'subtype':info.subtype,'sampleRate':info.samplerate,"
                                    "'channels':info.channels,'frames':info.frames,'duration':info.duration,'peak':float(np.max(np.abs(audio)))}))",
                                    str(path)], capture_output=True, text=True, encoding="utf-8", timeout=30, check=True)
                                return json.loads(probe.stdout)

                            input_peak = probe_audio(audio)["peak"]
                            assert input_peak > 1e-5
                            exports = []
                            for output_format, output_rate, bit_depth in (("wav", 48000, "PCM_24"), ("flac", 44100, "PCM_16")):
                                result = page.evaluate("""async ({format, sampleRate, bitDepth, exportDir}) => {
                                    const editor = document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('editor');
                                    editor.setMasterVolume(0.5);
                                    return await editor.exportMix({format, exportDir, fileName: 'nova-final-' + format,
                                        audioParams: {wavBitDepth: bitDepth, flacBitDepth: bitDepth, sampleRate, peakProtection: false}});
                                }""", {"format": output_format, "sampleRate": output_rate, "bitDepth": bit_depth,
                                        "exportDir": str(run_dir / "encoded")})
                                path = Path(result["path"]).resolve(strict=True)
                                assert path.parent == (run_dir / "encoded").resolve()
                                metadata = probe_audio(path)
                                assert metadata["format"].lower() == output_format and metadata["subtype"] == bit_depth, metadata
                                assert metadata["sampleRate"] == output_rate and metadata["channels"] == 2, metadata
                                assert metadata["duration"] >= duration and metadata["peak"] > 1e-5, metadata
                                assert abs(metadata["duration"] - report["nativeRenderDuration"]) <= 1 / output_rate, (
                                    "The final encode did not retain the measured native plugin render duration")
                                assert abs(metadata["peak"] / input_peak - 0.5) < 0.005, (
                                    "Master gain was lost or applied more than once in the native-to-Python export")
                                exports.append({"path": str(path), **metadata})
                            return {"files": exports, "inputPeak": input_peak,
                                    "nativeRenderDuration": report["nativeRenderDuration"], "masterGainAppliedOnce": True}

                        case("native_mix_to_python_wav_and_flac_encoding", encoded_exports)
                    if not args.skip_screenshot:
                        screenshot = run_dir / "editor-real-tauri.png"
                        page.screenshot(path=str(screenshot), full_page=True)
                        report["editorScreenshot"] = str(screenshot)
                except Exception as error:
                    if runtime_python is not None:
                        raise
                    report["screenshotWarning"] = str(error)
            try:
                invoke("close_current_window")
            except Exception:
                pass
    except Exception as error:
        report["fatalError"] = str(error)
        print(f"FAIL {error}", flush=True)
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if process is not None:
            if process.poll() is None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    report["forcedApplicationTermination"] = True
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], timeout=15,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            report["applicationExitCode"] = process.returncode
        report["consoleErrors"] = console_errors[:32]
        report["passed"] = len(report["cases"])
        report["failed"] = int("fatalError" in report) + int(report.get("applicationExitCode", 0) != 0)
        path = run_dir / "report.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{report['passed']} passed; {report['failed']} failed; report: {path}", flush=True)
    return int(report["failed"] != 0)


if __name__ == "__main__":
    sys.exit(main())
