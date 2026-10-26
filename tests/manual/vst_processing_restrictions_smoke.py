"""Verify VST3 insert/export restrictions in the isolated application helper.

Use only the vendored fixture. No device, installed plugin, desktop window or
user project is opened. Each scenario receives its own helper environment.
The SDK fixture itself aborts on a forbidden construction or mode transition.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from typing import Any

from vst_host_smoke import Host, read_float_wav


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, type=Path)
    fixture = parser.add_mutually_exclusive_group(required=True)
    fixture.add_argument("--fixture-library", type=Path)
    fixture.add_argument("--bundle", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    executable = args.host.resolve(strict=True)
    run_dir = args.output_dir.resolve() / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + f"-{os.getpid()}"
    )
    run_dir.mkdir(parents=True, exist_ok=False)
    if args.fixture_library:
        bundle = run_dir / "Restrictions.vst3"
        if sys.platform == "win32":
            location = "x86_64-win/Restrictions.vst3"
        elif sys.platform == "darwin":
            location = "MacOS/Restrictions"
        elif sys.platform == "linux":
            machine = platform.machine().lower()
            architecture = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(machine)
            assert architecture, f"Unsupported fixture architecture: {machine}"
            location = f"{architecture}-linux/Restrictions.so"
        else:
            raise RuntimeError(f"Unsupported fixture platform: {sys.platform}")
        binary = bundle / "Contents" / location
        binary.parent.mkdir(parents=True)
        shutil.copy2(args.fixture_library.resolve(strict=True), binary)
    else:
        bundle = args.bundle.resolve(strict=True)
    report: dict[str, Any] = {
        "host": str(executable), "bundle": str(bundle), "cases": [],
        "desktopLaunched": False, "audioStreamOpened": False, "realPluginUsed": False,
        "limitations": ["Empty-source projects validate mode admission and state preservation; SDK tests cover sample processing."],
    }

    def scenario(mode: str) -> None:
        environment = os.environ.copy()
        environment["PYMSS_VST_FIXTURE_BEHAVIOR"] = mode
        sandbox = run_dir / f"unused-data-{mode.replace(',', '-')}"
        environment["PYMSS_STUDIO_DATA_ROOT"] = str(sandbox)
        scanned = subprocess.run(
            [str(executable), "--plugin-scan", str(bundle)], env=environment,
            capture_output=True, encoding="utf-8", errors="replace", timeout=15, check=True,
        )
        descriptor = next(p for p in json.loads(scanned.stdout)["plugins"] if not p["isInstrument"])
        slot = dict(id="effect", format="vst3", pluginId=descriptor["id"], name=descriptor["name"],
                    path=str(bundle), bypassed=False)
        project = dict(id="restrictions", name="Audio", sources=[],
                       tracks=[dict(id="voice", clips=[])], pluginChain=[slot])
        helper = Host(executable, env=environment)
        # No sources are decoded. The protocol still requires an existing executable path.
        load_args = dict(project=project, ffmpeg=str(executable))
        try:
            if mode in {"only-ara", "only-offline"}:
                token = "OnlyARA" if mode == "only-ara" else "OnlyOfflineProcess"
                for offline in (False, True):
                    error = helper.request("load", **load_args, offline=offline, expect_error=True)
                    assert token in error["error"], error
                bypassed = copy.deepcopy(project)
                bypassed["pluginChain"][0]["bypassed"] = True
                loaded = helper.request("load", project=bypassed, ffmpeg=str(executable))
                assert len(loaded["unavailablePlugins"]) == 1, loaded
                assert token in loaded["unavailablePlugins"][0]["message"], loaded
                assert helper.request("snapshot")["states"] == {}, "Invented state for unavailable plugin"
                helper.request("update", **load_args, expect_error=True)
                assert helper.request("status")["sessionId"] == loaded["sessionId"]
            elif mode.startswith("only-rt"):
                preview = helper.request("load", **load_args, offline=False)
                helper.request("param", slotId="effect", paramId="7", value=1.25)
                helper.request("monitor", suppressedTrackIds=["voice"])
                before = helper.request("snapshot")
                output = run_dir / f"forbidden-{mode.replace(',', '-')}.wav"
                error = helper.request("render", path=str(output), expect_error=True)
                assert "OnlyRT" in error["error"], error
                assert not output.exists(), "Forbidden export left a partial WAV"
                update = helper.request("update", **load_args, offline=True, expect_error=True)
                assert "OnlyRT" in update["error"], update
                status = helper.request("status")
                assert status["sessionId"] == preview["sessionId"] and status["offline"] is False, status
                assert status["suppressedTrackIds"] == ["voice"] and status["error"] is None, status
                assert helper.request("snapshot")["states"] == before["states"]
                assert helper.request("parameters", slotId="effect")["values"]["7"] == 1.25
                # Recovery remains usable after refusal; bypass allows an ordinary export.
                bypassed = copy.deepcopy(project)
                bypassed["pluginChain"][0]["bypassed"] = True
                helper.request("update", project=bypassed, ffmpeg=str(executable))
                output = run_dir / f"bypassed-{mode.replace(',', '-')}.wav"
                helper.request("render", path=str(output))
                read_float_wav(output)
            else:
                assert mode == "no-offline"
                loaded = helper.request("load", **load_args, offline=True)
                assert loaded["offline"] is True
                output = run_dir / "allowed-downmix.wav"
                helper.request("render", path=str(output))
                read_float_wav(output)
                # NoOfflineProcess concerns direct offline processing, not normal downmix.
                preview = helper.request("update", **load_args, offline=False)
                helper.request("render", path=str(run_dir / "allowed-from-preview.wav"))
                assert helper.request("status")["sessionId"] == preview["sessionId"]
            helper.request("shutdown")
            assert helper.process.wait(timeout=10) == 0, helper.errors
            assert not sandbox.exists(), "Helper entered desktop startup"
        finally:
            helper.close()

    for mode in ("only-ara", "only-offline", "only-rt", "only-rt,reject-reactivate", "no-offline"):
        try:
            scenario(mode)
            report["cases"].append(dict(mode=mode, status="passed"))
            print(f"PASS {mode}", flush=True)
        except Exception as error:
            report["cases"].append(dict(mode=mode, status="failed", error=str(error)))
            print(f"FAIL {mode}: {error}", file=sys.stderr, flush=True)
    report["passed"] = sum(case["status"] == "passed" for case in report["cases"])
    (run_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Report: {run_dir / 'report.json'}", flush=True)
    return 0 if report["passed"] == len(report["cases"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
