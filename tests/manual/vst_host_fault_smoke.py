"""Verify actual plugin delay, nonlinear chain order, and isolated host faults.

Build the vendored fixture first. This script requires only Python's standard
library and writes every generated file beneath a new --artifacts directory.
Faults terminate only the dedicated host process and its descendants. It does
not exercise the Tauri UI or claim to verify the UI process's fault handling.
"""
from __future__ import annotations

import argparse
import base64
import copy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import queue
import shutil
import struct
import subprocess
import threading
import time
import wave


class HostExited(RuntimeError):
    pass


def terminate_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=10, check=False)
    else:
        import signal
        os.killpg(process.pid, signal.SIGKILL)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


class Host:
    def __init__(self, executable: Path, behavior: str = "") -> None:
        env = os.environ.copy()
        env.pop("PYMSS_VST_FIXTURE_BEHAVIOR", None)
        if behavior:
            env["PYMSS_VST_FIXTURE_BEHAVIOR"] = behavior
        self.process = subprocess.Popen(
            [str(executable), "--audio-host"], env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
        self.responses: queue.Queue[dict | Exception] = queue.Queue()
        self.sequence = 0
        self.project_id: str | None = None
        self.stderr: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._errors, daemon=True).start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            try:
                self.responses.put(json.loads(line))
            except ValueError as error:
                self.responses.put(error)
        self.responses.put(HostExited("The dedicated host closed its response stream"))

    def _errors(self) -> None:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            self.stderr.append(line.rstrip())
            del self.stderr[:-16]

    def request(self, op: str, timeout: float = 20, **arguments: object) -> dict:
        self.sequence += 1
        message = {"id": self.sequence, "op": op, **arguments}
        if self.project_id:
            message.setdefault("projectId", self.project_id)
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        try:
            response = self.responses.get(timeout=timeout)
        except queue.Empty as error:
            raise TimeoutError(f"The dedicated host timed out during {op}") from error
        if isinstance(response, Exception):
            raise response
        assert response.get("id") == self.sequence, f"Unexpected response sequence: {response}"
        assert response.get("ok") is True, f"Host rejected {op}: {response}"
        if op in {"load", "update"}:
            self.project_id = arguments["project"]["id"]
        return response["result"]

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                self.request("shutdown", timeout=2)
                self.process.wait(timeout=3)
            except Exception:
                terminate_tree(self.process)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()

    def __enter__(self) -> Host:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def read_float_wav(path: Path) -> tuple[int, list[tuple[float, float]]]:
    raw = path.read_bytes()
    assert raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"
    position, fmt, samples = 12, None, None
    while position + 8 <= len(raw):
        kind, size = struct.unpack_from("<4sI", raw, position)
        start = position + 8
        if kind == b"fmt ":
            fmt = struct.unpack_from("<HHIIHH", raw, start)
            if fmt[0] == 0xFFFE:
                fmt = (struct.unpack_from("<I", raw, start + 24)[0], *fmt[1:])
        elif kind == b"data":
            samples = raw[start:start + size]
        position = start + size + (size & 1)
    assert fmt is not None and samples is not None
    assert (fmt[0], fmt[1], fmt[5]) == (3, 2, 32), f"Unexpected output format: {fmt}"
    frames = list(struct.iter_unpack("<ff", samples))
    assert all(math.isfinite(value) for frame in frames for value in frame)
    return fmt[2], frames


def write_pcm(path: Path, frames: list[tuple[int, int]], rate: int = 48000) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setparams((2, 2, rate, 0, "NONE", "not compressed"))
        stream.writeframes(b"".join(struct.pack("<hh", *frame) for frame in frames))


def assert_audio(actual: list[tuple[float, float]], expected: list[tuple[float, float]]) -> float:
    assert len(actual) == len(expected), f"Frame count {len(actual)} differs from {len(expected)}"
    error = max((abs(a - e) for frame, target in zip(actual, expected) for a, e in zip(frame, target)), default=0)
    assert error < 2e-6, f"Maximum sample error {error} exceeded tolerance"
    return error


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=Path, required=True, help="Built isolated native host executable")
    parser.add_argument("--fixture", type=Path, required=True, help="Built fixture DLL or existing fixture VST3 bundle")
    parser.add_argument("--artifacts", type=Path, default=root / ".tmp/vst-host-fault-smoke")
    parser.add_argument("--ffmpeg", type=Path, default=Path(shutil.which("ffmpeg") or "ffmpeg"))
    args = parser.parse_args()
    host_path, fixture, ffmpeg = (path.resolve(strict=True) for path in (args.host, args.fixture, args.ffmpeg))
    if os.name == "nt":
        import ctypes
        # Inherited only by this script's children: faults must not open a WER dialog.
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    directory = args.artifacts.resolve() / f"{stamp}-{os.getpid()}"
    directory.mkdir(parents=True, exist_ok=False)
    if fixture.is_file():
        assert os.name == "nt", "A raw fixture DLL requires Windows; supply a bundle otherwise"
        bundle = directory / "FaultFixture.vst3"
        binary = bundle / "Contents/x86_64-win/FaultFixture.vst3"
        binary.parent.mkdir(parents=True)
        shutil.copy2(fixture, binary)
    else:
        bundle = fixture
    impulse_pcm = [(0, 0)] * 2048
    for index, values in {
        0: (8192, -16384), 2: (-4096, 8192), 17: (16384, 4096),
        255: (-8192, 12288), 256: (8192, -4096), 259: (4096, 4096),
        511: (-12288, -8192), 1023: (8192, 16384), 2047: (4096, -4096),
    }.items():
        impulse_pcm[index] = values
    impulse = directory / "impulse.wav"
    constant = directory / "constant.wav"
    write_pcm(impulse, impulse_pcm)
    write_pcm(constant, [(24576, -24576)] * 2048)
    impulse_float = [(left / 32768, right / 32768) for left, right in impulse_pcm]
    env = os.environ.copy()
    env.pop("PYMSS_VST_FIXTURE_BEHAVIOR", None)
    scanned = subprocess.run([str(host_path), "--plugin-scan", str(bundle)], env=env,
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=20, check=True)
    descriptor = next(plugin for plugin in json.loads(scanned.stdout)["plugins"] if plugin["name"] == "Fixture Gain")
    report = {"host": str(host_path), "fixture": str(fixture), "bundle": str(bundle),
              "artifacts": str(directory), "cases": [], "limitations": [
                  "Tests exercise dedicated native host processes; the Tauri UI process is not started.",
                  "Fixture modes are enabled only in each child's PYMSS_VST_FIXTURE_BEHAVIOR environment.",
              ]}

    def slot(identifier: str) -> dict:
        return {"id": identifier, "format": "vst3", "pluginId": descriptor["id"],
                "path": str(bundle), "name": descriptor["name"], "bypassed": False}

    def project(identifier: str, source: Path, chains: list[list[str]], master: list[str] | None = None) -> dict:
        return {
            "id": identifier, "name": "Plugin Host Verification", "masterVolume": 1, "masterPan": 0,
            "sources": [{"id": "source", "path": str(source), "duration": 2048 / 48000,
                         "sampleRate": 48000, "channels": 2}],
            "tracks": [{"id": f"track-{index}", "sourceId": "source", "volume": 1,
                        "pan": 0, "muted": False, "solo": False,
                        "pluginChain": [slot(name) for name in chain]} for index, chain in enumerate(chains)],
            "pluginChain": [slot(name) for name in master or []],
        }

    def load(host: Host, spec: dict) -> None:
        host.request("load", project=spec, ffmpeg=str(ffmpeg))

    def gain(host: Host, identifier: str, value: float) -> None:
        host.request("param", slotId=identifier, paramId="7", value=value)

    def render(host: Host, label: str) -> tuple[list[tuple[float, float]], dict]:
        path = directory / f"{label}.wav"
        result = host.request("render", path=str(path))
        rate, frames = read_float_wav(path)
        assert rate == 48000 and result["channels"] == 2
        assert len(frames) == 2048, f"Unexpected rendered frame count: {len(frames)}"
        assert abs(result["duration"] - len(frames) / rate) < 1 / rate
        return frames, result

    def recovery(label: str) -> dict:
        with Host(host_path) as host:
            load(host, project(f"recovery-{label}", constant, [["normal"]]))
            frames, _ = render(host, f"recovery-{label}")
            error = assert_audio(frames[16:-16], [(0.375, -0.375)] * (len(frames) - 32))
            return {"newHostPid": host.process.pid, "maxInteriorError": error}

    def case(name: str, action: object) -> None:
        started = time.monotonic()
        try:
            details = action()
            report["cases"].append({"name": name, "status": "passed", "details": details,
                                    "seconds": round(time.monotonic() - started, 3)})
            print(f"PASS {name}", flush=True)
        except Exception as error:
            report["cases"].append({"name": name, "status": "failed", "error": str(error),
                                    "seconds": round(time.monotonic() - started, 3)})
            print(f"FAIL {name}: {error}", flush=True)

    def parallel_delay() -> dict:
        with Host(host_path, "delay") as host:
            load(host, project("parallel-pdc", impulse, [[], ["delayed"]]))
            gain(host, "delayed", 1)
            frames, result = render(host, "parallel-pdc")
            error = assert_audio(frames, [(left * 2, right * 2) for left, right in impulse_float])
            assert frames[0] == (0.5, -1.0), "Latency trimming lost the original first sample"
            return {"mode": "delay", "pluginLatencySamples": 3, "maxSampleError": error,
                    "firstFrame": frames[0], "render": result}

    def master_delay() -> dict:
        with Host(host_path, "delay") as host:
            load(host, project("master-pdc", impulse, [[], ["track-delay"]], ["master-delay"]))
            gain(host, "track-delay", 1)
            gain(host, "master-delay", 1)
            frames, result = render(host, "master-pdc")
            error = assert_audio(frames, [(left * 2, right * 2) for left, right in impulse_float])
            assert frames[0] == (0.5, -1.0), "Master-chain compensation lost the first sample"
            return {"mode": "delay", "combinedLatencySamples": 6, "maxSampleError": error,
                    "firstFrame": frames[0], "render": result}

    def noncommutative_order() -> dict:
        with Host(host_path, "delay,clip") as host:
            spec = project("nonlinear-order", constant, [["boost", "reduce"]])
            load(host, spec)
            gain(host, "boost", 2)
            gain(host, "reduce", 0.5)
            first, _ = render(host, "boost-clip-reduce")
            first_error = assert_audio(first, [(0.5, -0.5)] * len(first))
            reordered = copy.deepcopy(spec)
            reordered["tracks"][0]["pluginChain"].reverse()
            host.request("update", project=reordered, ffmpeg=str(ffmpeg))
            second, _ = render(host, "reduce-boost-clip")
            second_error = assert_audio(second, [(0.75, -0.75)] * len(second))
            assert first[0] != second[0], "Reordering nonlinear processors produced identical output"
            return {"mode": "delay,clip", "firstOrder": ["boost", "reduce"],
                    "secondOrder": ["reduce", "boost"], "firstFrame": first[0],
                    "secondFrame": second[0], "maxErrors": [first_error, second_error]}

    def zero_frame_flush(host: Host) -> dict:
        gain(host, "fault", 1.25)
        snapshot = host.request("snapshot", timeout=5)
        state = base64.b64decode(snapshot["states"]["fault"], validate=True)
        assert state[:4] == b"SCV3" and struct.unpack_from("<I", state, 4)[0] == 8
        normalized = struct.unpack_from("<d", state, 8)[0]
        assert abs(normalized - 0.625) < 1e-12, "Zero-frame flush lost the pending gain"
        return {"normalizedGain": normalized, "safeBeforeFault": True}

    def process_crash() -> dict:
        with Host(host_path, "crash") as host:
            load(host, project("crash-child", constant, [["fault"]]))
            flushed = zero_frame_flush(host)
            crashed = False
            try:
                host.request("render", path=str(directory / "crash-partial.wav"), timeout=5)
            except (HostExited, BrokenPipeError):
                crashed = True
            assert crashed, "The crash fixture unexpectedly completed processing"
            code = host.process.wait(timeout=5)
            assert code != 0, "The crash fixture exited successfully"
            details = {"mode": "crash", "faultHostPid": host.process.pid, "exitCode": code,
                       "controllerPid": os.getpid(), "zeroFrameFlush": flushed}
        details["normalHostRecovery"] = recovery("after-crash")
        return details

    def process_hang() -> dict:
        with Host(host_path, "hang") as host:
            load(host, project("hung-child", constant, [["fault"]]))
            flushed = zero_frame_flush(host)
            timed_out = False
            started = time.monotonic()
            try:
                host.request("render", path=str(directory / "hang-partial.wav"), timeout=2)
            except TimeoutError:
                timed_out = True
            assert timed_out, "The hung fixture unexpectedly completed processing"
            assert host.process.poll() is None, "The hung child exited before explicit termination"
            terminate_tree(host.process)
            assert host.process.poll() is not None, "The hung child survived process-tree termination"
            details = {"mode": "hang", "faultHostPid": host.process.pid,
                       "exitCode": host.process.returncode, "controllerPid": os.getpid(),
                       "zeroFrameFlush": flushed, "terminatedProcessTree": True,
                       "timeoutSeconds": 2, "elapsedSeconds": round(time.monotonic() - started, 3)}
        details["normalHostRecovery"] = recovery("after-hang")
        return details

    case("actual_three_sample_parallel_delay_compensation", parallel_delay)
    case("master_chain_delay_and_original_start_sample", master_delay)
    case("nonlinear_chain_reorder_changes_audio", noncommutative_order)
    case("processor_crash_isolated_and_new_host_recovers", process_crash)
    case("processor_hang_timeout_tree_termination_and_recovery", process_hang)
    report["passed"] = all(case["status"] == "passed" for case in report["cases"])
    report_path = directory / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Report: {report_path}", flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
