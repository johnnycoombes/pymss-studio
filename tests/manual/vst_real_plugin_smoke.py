"""Check a user-supplied VST2/VST3 or registered macOS AU in the isolated host.

--plugin accepts a binary/bundle path or an au:/au3: component identifier.
Use --plugin-id for a multi-class bundle or VST2 shell. Native windows open only
with --editor. Parameter/audio comparison requires --param-id or --param-name;
choose a parameter that affects the generated 1 kHz tone. Stateful random effects
may need a larger --restore-tolerance. Reports distinguish omitted checks.
No installation is performed; all generated files go below --output-dir.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import os
import hashlib
import json
import math
from pathlib import Path
import shutil
import struct
import subprocess
import time
import wave

from vst_host_smoke import Host, read_float_wav, visible_windows


def scan_target(plugin: str) -> str:
    return plugin if plugin.startswith(("au:", "au3:")) else str(Path(plugin).resolve())


def select_plugin(plugins: list[dict], plugin_id: str | None) -> dict:
    candidates = [plugin for plugin in plugins if plugin.get("isInstrument") is not True]
    if plugin_id is not None:
        candidates = [plugin for plugin in candidates if plugin.get("id") == plugin_id]
    if len(candidates) != 1:
        choices = ", ".join(f"{plugin.get('id')} ({plugin.get('name')})" for plugin in candidates)
        raise ValueError(f"Expected one effect; found {len(candidates)}. Select --plugin-id. {choices}")
    descriptor = candidates[0]
    if descriptor.get("format") not in {"vst2", "vst3", "au"}:
        raise ValueError("The scanner returned an unsupported plugin format")
    if not descriptor.get("id") or not descriptor.get("path"):
        raise ValueError("The scanner returned an incomplete plugin reference")
    return descriptor


def rms_difference(left: list[tuple[float, float]], right: list[tuple[float, float]], *, same_length: bool) -> float:
    if same_length and len(left) != len(right):
        raise AssertionError(f"Restored render length changed: {len(left)} -> {len(right)} frames")
    count = min(len(left), len(right))
    if not count:
        raise AssertionError("The audio render contains no frames")
    return math.sqrt(sum((left[i][channel] - right[i][channel]) ** 2
                         for i in range(count) for channel in range(2)) / (count * 2))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=Path, required=True)
    parser.add_argument("--plugin", required=True)
    parser.add_argument("--plugin-id")
    parser.add_argument("--ffmpeg", type=Path, default=Path(shutil.which("ffmpeg") or "ffmpeg"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--editor", action="store_true")
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--param-name")
    selector.add_argument("--param-id")
    parser.add_argument("--param-value", type=float)
    parser.add_argument("--restore-tolerance", type=float, default=1e-4)
    args = parser.parse_args()
    if args.param_value is not None and not (args.param_id or args.param_name):
        parser.error("--param-value requires --param-id or --param-name")
    if not math.isfinite(args.restore_tolerance) or args.restore_tolerance < 0:
        parser.error("--restore-tolerance must be finite and nonnegative")
    if args.param_value is not None and not math.isfinite(args.param_value):
        parser.error("--param-value must be finite")
    root = args.output_dir.resolve() / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + str(os.getpid()))
    root.mkdir(parents=True, exist_ok=True)
    target = scan_target(args.plugin)
    scan = subprocess.run([str(args.host.resolve()), "--plugin-scan", target],
                          capture_output=True, text=True, encoding="utf-8", timeout=30, check=True)
    descriptor = select_plugin(json.loads(scan.stdout)["plugins"], args.plugin_id)
    source = root / "input.wav"
    rate = 48000
    with wave.open(str(source), "wb") as audio:
        audio.setparams((2, 2, rate, 0, "NONE", "not compressed"))
        audio.writeframes(b"".join(struct.pack("<hh", round(math.sin(i * 2 * math.pi * 1000 / rate) * 3276),
                                                 round(math.sin(i * 2 * math.pi * 1000 / rate) * 1638)) for i in range(rate)))
    slot = {"id": "real-plugin", "format": descriptor["format"], "pluginId": descriptor["id"],
            "name": descriptor["name"], "path": descriptor["path"], "bypassed": False}
    project = {"id": "real-plugin-smoke", "name": "Plugin compatibility", "masterVolume": 1, "masterPan": 0,
               "sources": [{"id": "source", "path": str(source), "name": source.name, "duration": 1,
                            "sampleRate": rate, "channels": 2}],
               "tracks": [{"id": "track", "sourceId": "source", "name": "Audio", "volume": 1, "pan": 0,
                           "muted": False, "solo": False, "pluginChain": [slot]}]}
    binary = Path(target) if not target.startswith(("au:", "au3:")) else None
    report = {"plugin": descriptor, "scanTarget": target,
              "binarySha256": file_sha256(binary) if binary is not None and binary.is_file() else None,
              "hostSha256": file_sha256(args.host.resolve()), "checks": [], "skipped": [], "status": "running"}
    host = Host(args.host.resolve())
    try:
        loaded = host.request("load", project=project, ffmpeg=str(args.ffmpeg.resolve()))
        report["sampleRate"] = loaded["sampleRate"]
        report["checks"].append("load")
        parameter_response = host.request("parameters", slotId=slot["id"])
        params = parameter_response["params"]
        report["parameters"] = params
        report["parameterCount"] = len(params)
        if args.editor:
            assert host.request("editor", slotId=slot["id"], open=True)["open"]
            if os.name == "nt":
                time.sleep(0.5)
                windows = visible_windows(host.process.pid)
                assert windows, "The plugin did not create a visible native window"
                report["windows"] = windows
            report["checks"].append("native_editor_open")
            assert not host.request("editor", slotId=slot["id"], open=False)["open"]
            assert host.request("editor", slotId=slot["id"], open=True)["open"]
            report["checks"].append("editor_reopen")
            host.request("editor", slotId=slot["id"], open=False)
        else:
            report["skipped"].append("native_editor: --editor was not supplied")
        initial = host.request("snapshot")["states"][slot["id"]]
        baseline = root / "baseline.wav"
        host.request("render", path=str(baseline))
        _, base = read_float_wav(baseline)
        assert base and max(abs(v) for frame in base for v in frame) > 0.001
        report["checks"].append("finite_non_silent_render")
        saved = initial
        if args.param_id or args.param_name:
            matches = [p for p in params if p["max"] > p["min"] and
                       (str(p["id"]) == args.param_id if args.param_id else args.param_name.casefold() in p["name"].casefold())]
            if len(matches) != 1:
                raise AssertionError(f"Expected one adjustable parameter; found {len(matches)}. Select --param-id")
            parameter = matches[0]
            # Pick the farther endpoint's interior to avoid requesting the current value.
            current = parameter_response["values"].get(str(parameter["id"]), parameter["default"])
            fraction = 0.2 if current > (parameter["min"] + parameter["max"]) / 2 else 0.8
            value = args.param_value if args.param_value is not None else parameter["min"] + (parameter["max"] - parameter["min"]) * fraction
            if not parameter["min"] <= value <= parameter["max"]:
                raise AssertionError("--param-value is outside the parameter range")
            host.request("param", slotId=slot["id"], paramId=parameter["id"], value=value)
            saved = host.request("snapshot")["states"][slot["id"]]
            assert initial != saved, "Changing a parameter did not change captured plugin state"
        else:
            report["skipped"].append("parameter_changes_audio: no parameter selector was supplied")
        # Compare fresh restored instances, avoiding live-instance smoothing history.
        changed_project = copy.deepcopy(project)
        changed_project["tracks"][0]["pluginChain"][0]["state"] = saved
        host.request("load", project=changed_project, ffmpeg=str(args.ffmpeg.resolve()))
        adjusted = root / "adjusted.wav"
        host.request("render", path=str(adjusted))
        _, changed = read_float_wav(adjusted)
        if args.param_id or args.param_name:
            difference = rms_difference(base, changed, same_length=False)
            assert difference > 1e-5, "Parameter changes did not reach audio processing"
            report["changedParameter"] = {"id": parameter["id"], "name": parameter["name"], "value": value}
            report["parameterRmsDifference"] = difference
            report["checks"].append("parameter_changes_audio")
        restored = copy.deepcopy(project)
        restored["tracks"][0]["pluginChain"][0]["state"] = saved
        host.request("load", project=restored, ffmpeg=str(args.ffmpeg.resolve()))
        restored_file = root / "restored.wav"
        host.request("render", path=str(restored_file))
        _, restored_frames = read_float_wav(restored_file)
        restoration_error = rms_difference(changed, restored_frames, same_length=True)
        assert restoration_error <= args.restore_tolerance, f"Restored plugin changed the result: RMS error={restoration_error}"
        report["stateRestoreRmsError"] = restoration_error
        report["stateRestoreTolerance"] = args.restore_tolerance
        report["checks"].append("state_restore_audio_parity")
        report["status"] = "passed"
    except Exception as error:
        report["status"] = "failed"
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        host.close()
        report["exitCode"] = host.process.returncode
        report["stderr"] = host.errors
        (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"plugin": descriptor["name"], "passed": len(report["checks"]),
                      "report": str(root / "report.json"), "exitCode": host.process.returncode}, ensure_ascii=False))


if __name__ == "__main__":
    main()
