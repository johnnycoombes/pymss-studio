"""Bounded native streaming regression with silent real-device playback.

Build the host, then pass --host and optionally --fixture-dll. The generated
15-second PCM sources exercise shared/independent sources and clip boundaries.
All files live beneath a new --output-dir child; application settings, installed
plugins, and projects are untouched. Only dedicated native host processes run.

The optional fixture is used in its actual three-sample delay mode, with unity
gain, so its offline comparison checks real delay compensation rather than the
fixture's default advertised-but-unimplemented latency. Device underruns are
reported as performance observations and are not required to be zero.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import time
from typing import Any, Callable
import wave

from vst_host_fault_smoke import Host, read_float_wav


RATE = 48000
CHUNK_FRAMES = 4 * RATE


def pcm(source: int, frame: int) -> tuple[int, int]:
    within = frame % CHUNK_FRAMES
    if within <= 2 or within >= CHUNK_FRAMES - 2:
        return 4096 + source * 512 + within % 5 * 16, -2048 - source * 256
    return (((frame // 17 + source * 7) % 33) - 16) * 64, (
        ((frame // 29 + source * 11) % 39) - 19
    ) * 32


def write_source(path: Path, source: int, duration: int) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setparams((2, 2, RATE, 0, "NONE", "not compressed"))
        for start in range(0, duration * RATE, 4096):
            count = min(4096, duration * RATE - start)
            block = bytearray(count * 4)
            for index in range(count):
                struct.pack_into("<hh", block, index * 4, *pcm(source, start + index))
            stream.writeframesraw(block)


def project(paths: list[Path], duration: int) -> dict[str, Any]:
    tracks = []
    for index, source in enumerate((0, 0, 0, 1, 2, 3)):
        tracks.append({
            "id": f"track-{index}", "sourceId": f"source-{source}",
            "volume": (0.125, 0.25, 0.125, 0.25, 0.125, 0.25)[index],
            "pan": (-0.5, 0.25, 0.5, -0.25, 0, 0.25)[index],
            "muted": False, "solo": False, "pluginChain": [],
        })

    def clips(track: int, values: list[tuple[float, float, float, float]]) -> None:
        tracks[track]["clips"] = [
            {"id": f"clip-{track}-{index}", "assetId": tracks[track]["sourceId"],
             "start": start, "offset": offset, "duration": length, "volume": volume,
             "fadeIn": 0, "fadeOut": 0, "muted": False, "locked": False}
            for index, (start, offset, length, volume) in enumerate(values)
        ]

    clips(1, [(0, 1, 4, 1), (4.125, 5.5, 4, 0.5), (8.125, 0, duration - 8.125, 0.75)])
    clips(2, [(0.25, 2, 7.5, 0.5), (8, 1, duration - 8, 1)])
    clips(4, [(0, 1, 7, 1), (7, 0, duration - 7, 0.5)])
    return {
        "id": "streaming-smoke", "name": "Native Streaming Verification",
        "masterVolume": 1, "masterPan": 0, "pluginChain": [],
        "sources": [{"id": f"source-{index}", "path": str(path), "duration": duration,
                     "sampleRate": RATE, "channels": 2} for index, path in enumerate(paths)],
        "tracks": tracks,
    }


def compare_render(path: Path, spec: dict[str, Any]) -> dict[str, Any]:
    rate, frames = read_float_wav(path)
    assert rate == RATE, f"Expected {RATE} Hz for exact samples, got {rate} Hz"
    assert len(frames) == round(spec["sources"][0]["duration"] * rate), len(frames)
    spans = []
    for track in spec["tracks"]:
        source = int(track["sourceId"].rsplit("-", 1)[1])
        clips = track.get("clips") or [{"start": 0, "offset": 0, "duration": len(frames) / rate, "volume": 1}]
        for clip in clips:
            gain = track["volume"] * clip["volume"] / 32768
            spans.append((source, round(clip["start"] * rate), round(clip["duration"] * rate),
                          round(clip["offset"] * rate), gain * (1 - max(0, track["pan"])),
                          gain * (1 + min(0, track["pan"]))))
    maximum = 0.0
    peak = 0.0
    for index, actual in enumerate(frames):
        left = right = 0.0
        for source, begin, length, offset, gain_left, gain_right in spans:
            if begin <= index < begin + length:
                value = pcm(source, offset + index - begin)
                left += value[0] * gain_left
                right += value[1] * gain_right
        error = max(abs(actual[0] - left), abs(actual[1] - right))
        assert error < 2e-6, f"Frame {index} ({index / rate:.6f}s): {actual} != {(left, right)}"
        maximum = max(maximum, error)
        peak = max(peak, abs(actual[0]), abs(actual[1]))
    return {"frames": len(frames), "sampleRate": rate, "maximumSampleError": maximum,
            "peak": peak, "tracks": 6, "sources": 4,
            "verifiedDecodeBoundariesSeconds": [4, 8, 12]}


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", type=Path, required=True)
    parser.add_argument("--fixture-dll", type=Path, help="Optional built vendored fixture DLL (Windows)")
    parser.add_argument("--ffmpeg", type=Path, default=Path(shutil.which("ffmpeg") or "ffmpeg"))
    parser.add_argument("--output-dir", type=Path, default=root / ".tmp/vst-host-streaming-smoke")
    parser.add_argument("--duration", type=int, choices=range(15, 21), default=15)
    parser.add_argument("--timeout", type=float, default=55, help="Whole-run budget, at most 60 seconds")
    args = parser.parse_args()
    if not 30 <= args.timeout <= 60:
        parser.error("--timeout must be between 30 and 60 seconds")
    executable, ffmpeg = args.host.resolve(strict=True), args.ffmpeg.resolve(strict=True)
    directory = args.output_dir.resolve() / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + f"-{os.getpid()}"
    )
    directory.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    deadline = started + args.timeout
    report: dict[str, Any] = {
        "host": str(executable), "ffmpeg": str(ffmpeg), "artifacts": str(directory),
        "durationSeconds": args.duration, "cases": [], "statusWindows": [],
        "limitations": ["Playback uses the default output device with masterVolume=0.",
                        "Underruns are observations; this smoke test makes no zero-underrun guarantee.",
                        "The test validates cursor timing and exact offline samples; it does not capture device audio."],
    }
    active: Host | None = None

    def budget() -> float:
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"Whole-run budget of {args.timeout}s expired"
        return remaining

    def request(operation: str, **arguments: Any) -> dict[str, Any]:
        assert active is not None
        return active.request(operation, timeout=min(15, budget()), **arguments)

    def status(label: str) -> dict[str, Any]:
        current = request("status")
        assert not current.get("error"), current
        assert math.isfinite(current["position"]), current
        assert 0 <= current["position"] <= args.duration, current
        # Store a linear, bounded status window in the report without console polling noise.
        report["statusWindows"].append({"window": label, "elapsed": round(time.monotonic() - started, 4),
                                        **{key: current[key] for key in (
                                            "position", "playing", "sessionId", "sampleRate", "underruns")}})
        assert len(report["statusWindows"]) <= 600, "Status window exceeded its bound"
        return current

    def wait_for(label: str, predicate: Callable[[dict[str, Any]], bool], seconds: float = 3) -> dict[str, Any]:
        until = min(deadline, time.monotonic() + seconds)
        current: dict[str, Any] = {}
        while time.monotonic() < until:
            current = status(label)
            if predicate(current):
                return current
            time.sleep(0.06)
        raise AssertionError(f"Timed out in {label}: {current}")

    def window(label: str, seconds: float, *, floor: float | None = None) -> list[dict[str, Any]]:
        until = min(deadline, time.monotonic() + seconds)
        samples = []
        while time.monotonic() < until:
            current = status(label)
            if samples:
                assert current["position"] >= samples[-1]["position"], f"Old frames moved cursor backwards in {label}: {samples[-1]} -> {current}"
            if floor is not None:
                assert current["position"] >= floor - 1 / current["sampleRate"], current
            samples.append(current)
            time.sleep(0.06)
        budget()
        return samples

    def case(name: str, action: Callable[[], Any]) -> None:
        began = time.monotonic()
        try:
            details = action()
            report["cases"].append({"name": name, "status": "passed", "details": details,
                                    "seconds": round(time.monotonic() - began, 3)})
            print(f"PASS {name}", flush=True)
        except Exception as error:
            report["cases"].append({"name": name, "status": "failed", "error": str(error),
                                    "seconds": round(time.monotonic() - began, 3)})
            print(f"FAIL {name}: {error}", flush=True)
            raise

    try:
        paths = [directory / f"source-{index}.wav" for index in range(4)]
        for index, path in enumerate(paths):
            write_source(path, index, args.duration)
        spec = project(paths, args.duration)
        active = Host(executable, "delay" if args.fixture_dll else "")

        def exact_render(label: str) -> dict[str, Any]:
            request("load", project=spec, ffmpeg=str(ffmpeg))
            path = directory / f"{label}.wav"
            rendered = request("render", path=str(path))
            details = compare_render(path, spec)
            assert abs(rendered["duration"] - details["frames"] / RATE) < 1 / RATE
            return {**details, "render": rendered}

        case("six_track_shared_independent_sources_exact_render", lambda: exact_render("dry-six-track"))

        if args.fixture_dll:
            assert os.name == "nt", "--fixture-dll requires Windows"
            binary = directory / "Fixture.vst3/Contents/x86_64-win/Fixture.vst3"
            binary.parent.mkdir(parents=True)
            shutil.copy2(args.fixture_dll.resolve(strict=True), binary)
            bundle = binary.parents[2]
            scanned = subprocess.run([str(executable), "--plugin-scan", str(bundle)], capture_output=True,
                                     text=True, encoding="utf-8", errors="replace", timeout=min(10, budget()), check=True)
            gain = next(plugin for plugin in json.loads(scanned.stdout)["plugins"] if plugin["name"] == "Fixture Gain")
            slot = {"id": "delayed-track", "format": "vst3", "pluginId": gain["id"],
                    "name": gain["name"], "path": str(bundle), "bypassed": False}
            spec["tracks"][0]["pluginChain"] = [slot]
            spec["pluginChain"] = [{**slot, "id": "delayed-master"}]

            def delayed_render() -> dict[str, Any]:
                request("load", project=spec, ffmpeg=str(ffmpeg))
                for identifier in ("delayed-track", "delayed-master"):
                    request("param", slotId=identifier, paramId="7", value=1)
                path = directory / "actual-delay-six-track.wav"
                request("render", path=str(path))
                return {**compare_render(path, spec), "actualDelayMode": True,
                        "trackDelaySamples": 3, "masterDelaySamples": 3}

            case("actual_delay_six_track_render_alignment", delayed_render)
            # Keep unity gain after the next explicit load, including during playback.
            saved = request("snapshot")["states"]
            spec["tracks"][0]["pluginChain"][0]["state"] = saved["delayed-track"]
            spec["pluginChain"][0]["state"] = saved["delayed-master"]

        spec["masterVolume"] = 0
        request("load", project=spec, ffmpeg=str(ffmpeg))

        def continuous() -> dict[str, Any]:
            request("play", position=0, loop=False)
            first = wait_for("continuous", lambda current: current["position"] > 0.08)
            anchor = time.monotonic()
            previous = first
            maximum_drift = 0.0
            until = min(deadline, anchor + 17)
            while previous["position"] < 12.4 and time.monotonic() < until:
                time.sleep(0.06)
                current = status("continuous")
                assert current["playing"], current
                assert current["sessionId"] == first["sessionId"], "Continuous playback unexpectedly rebuilt its session"
                assert current["position"] >= previous["position"], (previous, current)
                assert current["masterLevel"] == [0.0, 0.0], "Playback must remain silent"
                drift = current["position"] - first["position"] - (time.monotonic() - anchor)
                assert -1.25 < drift < 0.25, f"Cursor drift across decoded chunks: {drift:.3f}s"
                maximum_drift = max(maximum_drift, abs(drift))
                previous = current
            assert previous["position"] >= 12.4, previous
            return {"startPosition": first["position"], "endPosition": previous["position"],
                    "crossedChunkBoundariesSeconds": [4, 8, 12], "maximumWallClockDrift": maximum_drift,
                    "underruns": previous["underruns"]}

        case("silent_continuous_playback_crosses_three_decode_boundaries", continuous)

        def pause_and_seek() -> dict[str, Any]:
            request("play", position=0.2, loop=False)
            wait_for("before-paused-seeks", lambda current: current["position"] > 0.3)
            paused = request("pause")
            samples = window("pause", 0.24)
            assert all(not item["playing"] and item["position"] == paused["position"] for item in samples)
            for label, position in (("paused-near-seek", 0.35), ("paused-far-seek", 10.0)):
                sought = request("seek", position=position)
                samples = window(label, 0.24)
                assert all(not item["playing"] and abs(item["position"] - position) <= 1 / item["sampleRate"] for item in samples), (sought, samples)
            request("play", position=0.2, loop=False)
            wait_for("resume-before-seek", lambda current: current["position"] > 0.3)
            for position in (10.0, 0.5, 11.5, 2.0, 10.0):
                sought = request("seek", position=position)
                assert abs(sought["position"] - position) <= 1 / sought["sampleRate"], sought
            samples = window("rapid-playing-seeks", 0.9, floor=10)
            assert samples[-1]["playing"] and samples[-1]["position"] > 10.1, samples[-1]
            return {"pausedPosition": paused["position"], "latestSeekTarget": 10,
                    "finalPosition": samples[-1]["position"], "samples": len(samples)}

        case("pause_cached_far_and_rapid_seek_reject_old_frames", pause_and_seek)

        def mixer_updates() -> dict[str, Any]:
            before = status("before-mix")
            updated = copy.deepcopy(spec)
            updated["tracks"][0].update(volume=0.5, pan=0.5, solo=True)
            for index in range(3):
                updated["tracks"][0].update(volume=0.125 + index * 0.125, pan=-0.5 + index * 0.5)
                reply = request("update", project=updated, ffmpeg=str(ffmpeg))
                assert reply["playing"] and reply["sessionId"] == before["sessionId"], reply
                assert reply["position"] >= before["position"], reply
            samples = window("mix-updates", 0.6, floor=before["position"])
            assert samples[-1]["playing"] and samples[-1]["position"] > before["position"], samples[-1]
            return {"sessionPreserved": True, "beforePosition": before["position"],
                    "afterPosition": samples[-1]["position"], "updates": 3}

        case("volume_pan_solo_updates_preserve_playing_cursor", mixer_updates)

        def looping() -> dict[str, Any]:
            request("play", position=args.duration - 0.25, loop=True)
            wrapped = wait_for("loop-wrap", lambda current: current["playing"] and current["position"] < 0.5)
            samples = window("after-loop", 0.35)
            assert all(item["playing"] for item in samples)
            assert samples[-1]["position"] > wrapped["position"]
            request("pause")
            return {"wrappedPosition": wrapped["position"], "continuedPosition": samples[-1]["position"],
                    "underruns": samples[-1]["underruns"]}

        case("loop_wraps_and_continues_with_new_decode_generation", looping)
    except Exception as error:
        report["fatalError"] = str(error)
        print(f"STOP {error}", flush=True)
    finally:
        if active is not None:
            active.close()
            report["hostExitCode"] = active.process.returncode
            report["hostStderr"] = active.stderr
        report["elapsedSeconds"] = round(time.monotonic() - started, 3)
        report["passed"] = sum(item["status"] == "passed" for item in report["cases"])
        report["failed"] = sum(item["status"] == "failed" for item in report["cases"])
        if "fatalError" in report and not report["failed"]:
            report["failed"] += 1
        if report.get("hostExitCode", 0) != 0:
            report["failed"] += 1
        report_path = directory / "report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{report['passed']} passed; {report['failed']} failed; report: {report_path}", flush=True)
    return int(report["failed"] != 0)


if __name__ == "__main__":
    raise SystemExit(main())
