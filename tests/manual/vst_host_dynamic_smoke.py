"""Focused native VST3 regressions: revisions, state restore, and dynamic PDC.

Uses the opt-in vendored fixture modes. All processes and files belong to this
test; real-device playback is silent. No installed plugin or application setting
is changed. Build the host and fixture first, then pass their paths below.
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import time
import wave

from vst_host_fault_smoke import Host, read_float_wav


def component(snapshot: dict, slot: str) -> bytes:
    state = base64.b64decode(snapshot["states"][slot], validate=True)
    assert state[:4] == b"SCV3"
    length = struct.unpack_from("<I", state, 4)[0]
    return state[8:8 + length]


def gain(snapshot: dict, slot: str = "gain") -> float:
    return 2 * struct.unpack_from("<d", component(snapshot, slot))[0]


def until(host: Host, predicate, seconds: float = 8) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        status = host.request("status")
        if predicate(status):
            return status
        time.sleep(0.01)
    raise AssertionError(f"Timed out waiting for native state: {status}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=Path, required=True)
    parser.add_argument("--fixture-dll", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, default=Path(shutil.which("ffmpeg") or "ffmpeg"))
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    executable, ffmpeg = args.host.resolve(strict=True), args.ffmpeg.resolve(strict=True)
    directory = args.artifacts.resolve() / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + f"-{os.getpid()}")
    binary = directory / "Fixture.vst3/Contents/x86_64-win/Fixture.vst3"
    binary.parent.mkdir(parents=True)
    shutil.copy2(args.fixture_dll.resolve(strict=True), binary)
    bundle = binary.parents[2]
    scan = subprocess.run([str(executable), "--plugin-scan", str(bundle)], check=True, capture_output=True,
                          text=True, encoding="utf-8", timeout=20)
    descriptor = next(plugin for plugin in json.loads(scan.stdout)["plugins"] if plugin["name"] == "Fixture Gain")
    source = directory / "source.wav"
    with wave.open(str(source), "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(48000)
        stream.writeframes(struct.pack("<hh", 4096, -8192) * 24000)
    report = {"cases": [], "host": str(executable)}

    def project(silent: bool = False) -> dict:
        return {"id": "dynamic-smoke", "name": "Plugin compatibility", "masterVolume": 0 if silent else 1,
                "sources": [{"id": "source", "path": str(source), "duration": 0.5, "sampleRate": 48000, "channels": 2}],
                "tracks": [{"id": "wet", "sourceId": "source", "pluginChain": [
                    {"id": "gain", "format": "vst3", "pluginId": descriptor["id"], "name": descriptor["name"],
                     "path": str(bundle), "bypassed": False}]}]}

    def load(host: Host, spec: dict) -> dict:
        return host.request("load", project=spec, ffmpeg=str(ffmpeg))

    def render(host: Host, name: str, expected: tuple[float, float], *, exact_start: bool = False) -> dict:
        path = directory / (name + ".wav")
        result = host.request("render", path=str(path))
        rate, frames = read_float_wav(path)
        sample = frames[:min(len(frames), 1024)] if exact_start else frames[rate // 10:rate // 3]
        assert sample
        assert all(max(abs(frame[channel] - expected[channel]) for channel in (0, 1)) < 2e-6 for frame in sample), (
            f"Incorrect rendered audio at {name}: {sample[:12]} != {expected}")
        return result

    def case(name: str, function) -> None:
        try:
            details = function()
            report["cases"].append({"name": name, "status": "passed", "details": details})
            print(f"PASS {name}", flush=True)
        except Exception as error:
            report["cases"].append({"name": name, "status": "failed", "error": str(error)})
            print(f"FAIL {name}: {error}", flush=True)

    def revisions() -> dict:
        with Host(executable) as host:
            spec = project()
            initial = load(host, spec)
            assert initial["sessionId"] and initial["stateRevision"] == 0
            for _ in range(3):
                saved = host.request("snapshot")
                assert saved["sessionId"] == initial["sessionId"] and saved["stateRevision"] == 0
            changed = host.request("param", slotId="gain", paramId="7", value=1.5)
            assert changed["stateRevision"] > 0
            saved = host.request("snapshot")
            assert gain(saved) == 1.5 and saved["stateRevision"] == changed["stateRevision"]
            same = host.request("param", slotId="gain", paramId="7", value=1.5)
            assert same["stateRevision"] == saved["stateRevision"]
            spec["tracks"][0]["volume"] = 0.5
            updated = host.request("update", project=spec, ffmpeg=str(ffmpeg))
            assert updated["sessionId"] == initial["sessionId"] and updated["stateRevision"] == saved["stateRevision"]
            fresh = load(host, spec)
            assert fresh["sessionId"] != initial["sessionId"] and fresh["stateRevision"] == 0
            return {"stableSnapshotRevision": saved["stateRevision"], "newLoadResetsRevision": True}

    case("session_identity_revision_and_snapshot_stability", revisions)

    def gui_edits() -> dict:
        with Host(executable, "gui-edit") as host:
            load(host, project())
            host.request("editor", slotId="gain", open=True)
            status = until(host, lambda current: current["stateRevision"] > 0)
            assert status["openEditors"] == ["gain"]
            saved = host.request("snapshot")
            assert abs(gain(saved) - 1.2) < 1e-6
            assert host.request("snapshot")["stateRevision"] == saved["stateRevision"]
            host.request("editor", slotId="gain", open=False)
            closed = host.request("status")
            assert closed["openEditors"] == [] and closed["stateRevision"] > status["stateRevision"]
            return {"editedRevision": status["stateRevision"], "closedRevision": closed["stateRevision"]}

    case("gui_parameter_edit_and_window_close_revision", gui_edits)

    def opaque_gui_state() -> dict:
        with Host(executable, "gui-state") as host:
            initial = load(host, project())
            host.request("editor", slotId="gain", open=True)
            saved = host.request("snapshot")
            raw = base64.b64decode(saved["states"]["gain"])
            controller = 8 + struct.unpack_from("<I", raw, 4)[0] + 4
            assert struct.unpack_from("<d", raw, controller)[0] == 0.75
            assert saved["stateRevision"] == initial["stateRevision"]
            host.request("editor", slotId="gain", open=False)
            closed = host.request("snapshot")
            assert closed["stateRevision"] > saved["stateRevision"]
            return {"opaqueControllerStateCaptured": True, "closeMarksDirty": True}

    case("non_parameter_controller_state_close_fallback", opaque_gui_state)

    for mode, expected, index in (("gui-hidden", (0.09375, -0.1875), 8), ("delay+gui-bypass", (0.125, -0.25), 16)):
        def hidden_or_bypass(mode=mode, expected=expected, index=index) -> dict:
            with Host(executable, mode) as host:
                load(host, project())
                host.request("editor", slotId="gain", open=True)
                saved = host.request("snapshot")
                value = struct.unpack_from("<d", component(saved, "gain"), index)[0]
                assert value == (0.5 if index == 8 else 1.0)
                render(host, mode.replace("+", "-"), expected, exact_start=index == 16)
                return {"rawParameterReachedProcessor": True, "revision": saved["stateRevision"]}
        case(mode.replace("+", "_"), hidden_or_bypass)

    def state_restore() -> dict:
        with Host(executable) as host:
            spec = project(silent=True)
            spec["pluginChain"] = [{**spec["tracks"][0]["pluginChain"][0], "id": "master"}]
            initial = load(host, spec)
            original = host.request("snapshot")
            host.request("param", slotId="gain", paramId="7", value=1.5)
            host.request("param", slotId="master", paramId="7", value=1.25)
            host.request("seek", position=0.12)
            host.request("editor", slotId="gain", open=True)
            spec["tracks"][0]["pluginChain"][0]["state"] = original["states"]["gain"]
            spec["pluginChain"][0]["state"] = original["states"]["master"]
            restored = host.request("update", project=spec, ffmpeg=str(ffmpeg), stateRestoreSlotIds=["gain"])
            assert restored["sessionId"] != initial["sessionId"] and restored["position"] == 0.12
            assert not restored["playing"] and restored["openEditors"] == ["gain"]
            saved = host.request("snapshot")
            assert gain(saved) == 0.5 and gain(saved, "master") == 1.25
            host.request("play", position=0.1, loop=True)
            until(host, lambda current: current["position"] > 0.12)
            resumed = host.request("update", project=spec, ffmpeg=str(ffmpeg), stateRestoreSlotIds=["gain"])
            assert resumed["playing"] and resumed["loop"] and resumed["openEditors"] == ["gain"]
            return {"requestedSlotRestored": True, "otherLiveStatePreserved": True, "transportAndEditorRestored": True}

    case("explicit_slot_state_restore_preserves_other_live_instances", state_restore)

    def dynamic_latency() -> dict:
        with Host(executable, "dynamic-delay") as host:
            spec = project(silent=True)
            initial = load(host, spec)
            host.request("play", position=0, loop=True)
            until(host, lambda current: current["position"] > 0.1)
            host.request("editor", slotId="gain", open=True)
            host.request("param", slotId="gain", paramId="7", value=1.5)
            recovered = until(host, lambda current: current.get("error") or current["sessionId"] != initial["sessionId"])
            assert not recovered.get("error"), recovered
            assert recovered["playing"] and recovered["loop"] and recovered["openEditors"] == ["gain"]
            snapshot = host.request("snapshot")
            assert gain(snapshot) == 1.5
            host.request("pause")
            spec["masterVolume"] = 1
            spec["tracks"][0]["pluginChain"][0]["state"] = snapshot["states"]["gain"]
            spec["tracks"].append({"id": "dry", "sourceId": "source"})
            load(host, spec)
            render(host, "recovered-pdc-eleven", (0.3125, -0.625), exact_start=True)
            return {"newSession": recovered["sessionId"], "stateGain": 1.5, "elevenSamplePdc": True}

    case("dynamic_latency_single_rebuild_and_actual_pdc", dynamic_latency)

    def startup_latency() -> dict:
        with Host(executable, "startup-delay") as host:
            spec = project(silent=True)
            initial = load(host, spec)
            host.request("play", position=0, loop=True)
            recovered = until(host, lambda current: current.get("error") or current["sessionId"] != initial["sessionId"])
            assert not recovered.get("error") and recovered["playing"], recovered
            until(host, lambda current: current["position"] > 0.1)
            assert not host.request("status").get("error")
        # A fresh instance initially advertises three samples, then settles at
        # eleven only after its first positive process block. Offline preparation
        # must negotiate that same instance instead of recreating it forever.
        with Host(executable, "startup-delay") as host:
            spec = project()
            spec["tracks"].append({"id": "dry", "sourceId": "source"})
            load(host, spec)
            render(host, "startup-latency-pdc", (0.1875, -0.375), exact_start=True)
        return {"stableFirstProcessChangeRecovers": True, "offlineSameInstancePdc": True}

    case("stable_first_process_latency_negotiates_without_recreating_plugin", startup_latency)

    def unstable_latency() -> dict:
        with Host(executable, "unstable-delay") as host:
            initial = load(host, project(silent=True))
            host.request("play", position=0, loop=True)
            failed = until(host, lambda current: bool(current.get("error")))
            assert not failed["playing"] and "unstable" in failed["error"].lower(), failed
            assert failed["sessionId"] != initial["sessionId"]
            for _ in range(5):
                time.sleep(0.02)
                assert host.request("status")["sessionId"] == failed["sessionId"]
            return {"stoppedAfterOneRecovery": True, "error": failed["error"]}

    case("repeated_latency_change_stops_without_restart_loop", unstable_latency)

    def ordinary_process_failure() -> dict:
        with Host(executable, "process-error") as host:
            initial = load(host, project(silent=True))
            host.request("play", position=0, loop=True)
            failed = until(host, lambda current: bool(current.get("error")))
            assert not failed["playing"] and failed["sessionId"] == initial["sessionId"]
            time.sleep(0.1)
            assert host.request("status")["sessionId"] == initial["sessionId"]
            return {"ordinaryFailureDidNotRestart": True, "error": failed["error"]}

    case("ordinary_processor_failure_stops_without_automatic_recovery", ordinary_process_failure)

    def offline_failure_cleanup() -> dict:
        with Host(executable, "late-delay") as host:
            load(host, project())
            path = directory / "must-not-survive-partial.wav"
            try:
                host.request("render", path=str(path))
            except AssertionError as error:
                assert "latency changed" in str(error).lower(), error
            else:
                raise AssertionError("A mid-render latency change unexpectedly succeeded")
            assert not path.exists(), "A failed render left a partial output file"
            return {"partialOutputRemoved": True}

    case("mid_export_latency_change_fails_and_removes_partial_output", offline_failure_cleanup)
    report["passed"] = sum(item["status"] == "passed" for item in report["cases"])
    report["failed"] = len(report["cases"]) - report["passed"]
    (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"{report['passed']} passed; {report['failed']} failed; report: {directory / 'report.json'}", flush=True)
    return int(report["failed"] != 0)


if __name__ == "__main__":
    raise SystemExit(main())
