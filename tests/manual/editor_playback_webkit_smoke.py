"""Probe the real editor playback composable in the installed macOS WKWebView.

Run with a dedicated Vite server (HMR and file watching disabled):
    python3 tests/manual/editor_playback_webkit_smoke.py --url http://127.0.0.1:1460
Optional real assets, without editing the saved project:
    ... --project /path/to/project.json --offset 45

This exercises the audio engine, timeline mixer UI and a Tauri-like asset
scheme. It does not exercise Tauri itself or measure sound at the output device.
Results include media events, seeks, starts, context clocks and mixer levels.
"""
import argparse
import io
import json
import math
import shutil
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import wave

# Native runner is compiled only inside the temporary test fixture.
SWIFT_RUNNER = r'''// Native macOS WebKit runner for editor_playback_webkit_smoke.py.
import Cocoa
import WebKit

final class AssetHandler: NSObject, WKURLSchemeHandler {
    func webView(_ webView: WKWebView, start task: WKURLSchemeTask) {
        DispatchQueue.global(qos: .userInitiated).async { self.respond(task) }
    }
    private func respond(_ task: WKURLSchemeTask) {
        do {
            let url = task.request.url!
            let query = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
            let args = Dictionary(uniqueKeysWithValues: query.map { ($0.name, $0.value ?? "") })
            let data: Data
            if let index = args["previewIndex"], let rate = args["sampleRate"], let channels = args["channels"] {
                let process = Process()
                process.executableURL = URL(fileURLWithPath: CommandLine.arguments[2])
                var arguments = ["-nostdin", "-v", "error", "-threads", "1", "-copyts", "-start_at_zero"]
                if index == "full" {
                    arguments += ["-i", url.path, "-map", "0:a:0", "-vn"]
                } else {
                    let start = (Int(index) ?? 0) * 4, warm = max(0, (Int(index) ?? 0) * 4 - 1)
                    if start > 0 { arguments += ["-ss", String(warm)] }
                    arguments += ["-i", url.path, "-map", "0:a:0", "-vn", "-af",
                        "atrim=start=\(warm),asetpts=PTS-\(warm)/TB,aresample=\(rate):first_pts=0,atrim=start=\(start - warm):duration=4,asetpts=PTS-STARTPTS", "-t", "4"]
                }
                process.arguments = arguments + ["-ar", rate, "-ac", channels, "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1"]
                let pipe = Pipe()
                process.standardOutput = pipe
                try process.run()
                data = pipe.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                guard process.terminationStatus == 0 else { throw NSError(domain: "PreviewDecode", code: 1) }
            } else { data = try Data(contentsOf: URL(fileURLWithPath: url.path)) }
            let length = data.count
            var start = 0
            var end = length - 1
            var status = 200
            if let range = task.request.value(forHTTPHeaderField: "Range") {
                let parts = range.replacingOccurrences(of: "bytes=", with: "")
                    .split(separator: "-", omittingEmptySubsequences: false)
                start = Int(parts[0]) ?? 0
                if parts.count > 1 { end = Int(parts[1]) ?? end }
                // Match Tauri's single-range response size limit.
                end = min(end, start + 1000 * 1024 - 1)
                status = 206
            }
            // A successful decode beyond the last sample returns an empty
            // response, matching Tauri IPC's EOF result.
            guard (length == 0 && status == 200) || (start >= 0 && end >= start && end < length) else {
                throw NSError(domain: "AudioProbe", code: 416)
            }
            let mime = url.pathExtension.lowercased() == "flac" ? "audio/flac" : "audio/wav"
            var headers = [
                "Content-Type": mime, "Access-Control-Allow-Origin": "*",
                "Accept-Ranges": "bytes", "Content-Length": String(end - start + 1),
                "Access-Control-Expose-Headers": "content-range",
            ]
            if status == 206 { headers["Content-Range"] = "bytes \(start)-\(end)/\(length)" }
            task.didReceive(HTTPURLResponse(url: url, statusCode: status,
                                           httpVersion: "HTTP/1.1", headerFields: headers)!)
            task.didReceive(data.subdata(in: start..<end + 1))
            task.didFinish()
        } catch { task.didFailWithError(error) }
    }
    func webView(_ webView: WKWebView, stop task: WKURLSchemeTask) {}
}

final class Probe: NSObject, WKScriptMessageHandler, WKNavigationDelegate {
    var view: WKWebView!
    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        print(message.body)
        if let text = message.body as? String, text == "DONE" { exit(0) }
    }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        // Vite may still be resolving module imports after navigation finishes.
        webView.evaluateJavaScript("""
            const ready = setInterval(() => {
                if (typeof window.run === 'function') { clearInterval(ready); window.run(); }
            }, 50); void 0
            """) { _, error in
            if let error = error { print(error); exit(1) }
        }
    }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        print(error)
        exit(1)
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let probe = Probe()
let configuration = WKWebViewConfiguration()
configuration.mediaTypesRequiringUserActionForPlayback = []
configuration.setURLSchemeHandler(AssetHandler(), forURLScheme: "asset")
configuration.userContentController.add(probe, name: "probe")
probe.view = WKWebView(frame: NSRect(x: 0, y: 0, width: 800, height: 600), configuration: configuration)
probe.view.navigationDelegate = probe
let window = NSWindow(contentRect: probe.view.frame, styleMask: [.titled], backing: .buffered, defer: false)
window.contentView = probe.view
window.orderFront(nil)
probe.view.load(URLRequest(url: URL(string: CommandLine.arguments[1])!))
DispatchQueue.main.asyncAfter(deadline: .now() + 90) { print("TIMEOUT"); exit(1) }
app.run()
'''

HTML = """<!doctype html><html><body><div id="app"></div>
<script>window.__TAURI_INTERNALS__ = {
  convertFileSrc: path => 'asset://localhost' + path,
  invoke: async (command, args) => {
    if (command !== 'editor_preview_chunk') return;
    const url = new URL('asset://localhost'); url.pathname = args.path;
    url.search = new URLSearchParams({ previewIndex: args.index, sampleRate: args.sampleRate, channels: args.channels });
    const response = await fetch(url);
    if (!response.ok) throw new Error('Chunk fetch failed');
    return response.arrayBuffer();
  },
};</script>
<script type="module">
import { createApp, reactive, ref, h } from 'vue'
import { createPinia } from 'pinia'
import { useEditorPlayback } from '/src/composables/useEditorPlayback.ts'
import { createPreviewChunkReader, EditorTimelinePlayback } from '/src/utils/editorAudio.ts'
import EditorMixer from '/src/components/editor/EditorMixer.vue'
import i18n from '/src/i18n/index.ts'
const project = __PROJECT__;
const contexts = [], scheduled = [];
const NativeContext = window.AudioContext;
const nativeFrame = window.requestAnimationFrame.bind(window);
let suppressFrames = false, activeTransport;
window.requestAnimationFrame = callback => nativeFrame(time => { if (!suppressFrames) callback(time); });
window.AudioContext = class extends NativeContext {
  constructor(...args) { super(...args); contexts.push(this); }
  createBufferSource() {
    const node = super.createBufferSource(), start = node.start.bind(node);
    const connect = node.connect.bind(node); let destination;
    node.connect = target => { destination = target; return connect(target); };
    node.start = (when, offset, duration) => {
      scheduled.push({ when, offset, duration, clipId: destination?.probeOutput?.probeClipId });
      start(when, offset, duration);
    };
    return node;
  }
  createGain() {
    const node = super.createGain(), connect = node.connect.bind(node);
    node.connect = target => { node.probeOutput = target; return connect(target); };
    return node;
  }
};
const timelineStart = EditorTimelinePlayback.prototype.start;
EditorTimelinePlayback.prototype.start = function(entries, offset, duration) {
  activeTransport = this;
  for (const entry of entries) entry.output.probeClipId = entry.clip.id;
  return timelineStart.call(this, entries, offset, duration);
};
const editor = reactive({
  session: project, sourceMap: new Map(project.sources.map(source => [source.id, source])),
  duration: __DURATION__, masterVolume: 0.35, masterPan: 0,
});
let playback;
const scrollEl = ref(null);
const app = createApp({ setup() {
  playback = useEditorPlayback({ editor, scrollEl, trackHeaderWidth: 180 });
  return () => h(EditorMixer, {
    tracks: project.tracks, sourceMap: editor.sourceMap,
    selectedTrackId: null, selectedClipId: null,
    currentTime: playback.currentTime.value, duration: editor.duration,
    pixelsPerSecond: 4, trackLevels: playback.trackLevels.value,
    style: 'height: 560px; width: 780px',
    onScrollReady: element => { scrollEl.value = element; },
    onSeek: time => playback.seek(time),
  });
} });
app.use(createPinia()).use(i18n).mount('#app');
const send = value => window.webkit.messageHandlers.probe.postMessage(JSON.stringify(value));
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
async function verifyPcmContinuity() {
  const results = [];
  for (const rate of [44100, 48000]) {
    const url = new URL('asset://localhost'); url.pathname = __PCM_FIXTURE__;
    url.search = new URLSearchParams({ previewIndex: 'full', sampleRate: rate, channels: 1 });
    const samples = new Float32Array(await (await fetch(url)).arrayBuffer());
    for (const position of [
      { start: 0, offset: 0, seek: 0 },
      { start: 0, offset: 0, seek: 1.234567 },
      { start: 0, offset: 1.234567, seek: 0 },
      { start: 0.321098, offset: 1.234567, seek: 0.654321 },
      { start: 0.000013, offset: 0.000004, seek: 0 },
    ]) {
      const clip = { id: 'pcm', start: position.start, offset: position.offset,
        duration: 9 - position.offset, volume: 1, fadeIn: 0, fadeOut: 0 };
      async function render(split) {
        const ctx = new OfflineAudioContext(1, rate * 11, rate), errors = [];
        let transport;
        if (split) {
          transport = new EditorTimelinePlayback(ctx, createPreviewChunkReader(ctx), error => errors.push(String(error)));
          await transport.start([{ clip, source: { path: __PCM_FIXTURE__, duration: 9,
            sampleRate: 44100, channels: 1 }, output: ctx.destination }], position.seek, clip.start + clip.duration);
          for (let retry = 0; transport.bufferedUntil < clip.start + clip.duration - 1 / rate && retry < 500; retry++) {
            await transport.pump(); await wait(10);
          }
          if (errors.length || transport.bufferedUntil < clip.start + clip.duration - 1 / rate) {
            transport.dispose(); throw new Error(errors.join('; ') || 'PCM prefetch timed out');
          }
        } else {
          const buffer = ctx.createBuffer(1, samples.length, rate); buffer.copyToChannel(samples, 0);
          const node = ctx.createBufferSource(); node.buffer = buffer; node.connect(ctx.destination);
          const start = Math.round(clip.start * rate), seek = Math.round(position.seek * rate);
          const offset = Math.round(clip.offset * rate) + Math.max(0, seek - start);
          node.start(0.06 + Math.max(0, start - seek) / rate, offset / rate,
            Math.min(Math.round((clip.start + clip.duration) * rate) - Math.max(start, seek), samples.length - offset) / rate);
        }
        try { return (await ctx.startRendering()).getChannelData(0); }
        finally { transport?.dispose(); }
      }
      const whole = await render(false), chunks = await render(true);
      let maxError = 0;
      for (let frame = 0; frame < whole.length; frame++) maxError = Math.max(maxError, Math.abs(whole[frame] - chunks[frame]));
      if (maxError > 1e-5) throw new Error(`PCM continuity failed at ${rate} Hz, ${JSON.stringify(position)}: ${maxError}`);
      results.push({ rate, ...position, maxError });
    }
  }
  send({ pcmContinuity: 'passed', results });
}
window.run = async () => {
  try {
    await verifyPcmContinuity();
    const requestedAt = performance.now();
    if (!await playback.requestPlay(__OFFSET__)) throw new Error(playback.playbackError.value || 'Start failed');
    send({ startupMilliseconds: performance.now() - requestedAt });
    const initialSchedule = scheduled.slice();
    const initialClips = project.tracks.flatMap(track => track.clips)
      .filter(clip => clip.start < __OFFSET__ + 4 && clip.start + clip.duration > __OFFSET__);
    const anchors = initialClips.map(clip => {
      const first = initialSchedule.find(item => item.clipId === clip.id);
      if (!first) throw new Error('Missing initial clip schedule: ' + clip.id);
      return first.when - Math.max(clip.start, __OFFSET__) + __OFFSET__;
    });
    if (anchors.length && Math.max(...anchors) - Math.min(...anchors) > 1e-6) {
      throw new Error('Track or later clip started at the wrong project time');
    }
    send({ timing: 'passed', anchorSpreadSeconds: anchors.length ? Math.max(...anchors) - Math.min(...anchors) : 0 });
    for (let index = 0; index < 25; index++) {
      if ([8, 12, 17].includes(index)) {
        const track = editor.session.tracks[0], clip = track.clips[0];
        track.clips[0] = { ...clip, volume: index === 8 ? 0.5 : 0.4,
          fadeIn: index === 17 ? 0 : clip.duration,
          fadeOut: index === 17 ? 0 : clip.duration };
      }
      await wait(400);
      send({ wall: performance.now() / 1000, time: playback.currentTime.value,
        status: playback.status.value, error: playback.playbackError.value,
        level: playback.level.value,
        scheduled: scheduled.length,
        contexts: contexts.map(ctx => ({ state: ctx.state, rate: ctx.sampleRate, time: ctx.currentTime })),
      });
    }
    playback.seek(__OFFSET__ + 2);
    await wait(150);
    playback.requestPause();
    const held = playback.currentTime.value;
    await wait(200);
    if (playback.currentTime.value !== held) throw new Error('Pause did not hold position');
    if (!await playback.requestPlay()) throw new Error('Resume failed');
    for (let retry = 0; retry < 20 && playback.currentTime.value <= held; retry++) await wait(100);
    if (playback.currentTime.value <= held) throw new Error('Resume did not advance within 2 seconds');
    playback.loop.value = true;
    playback.seek(editor.duration - 0.1);
    await wait(1500);
    if (playback.status.value !== 'playing' || playback.currentTime.value > 2) {
      throw new Error('Loop did not restart at the beginning');
    }
    suppressFrames = true;
    playback.loop.value = false;
    playback.seek(editor.duration - 0.1);
    await wait(1500);
    if (playback.status.value !== 'paused' || playback.currentTime.value !== 0) {
      throw new Error('Completion depended on animation frames');
    }
    playback.loop.value = true;
    if (!await playback.requestPlay(editor.duration - 0.1)) throw new Error('Background loop start failed');
    await wait(1500);
    if (playback.status.value !== 'playing' || activeTransport.currentTime > 2) {
      throw new Error('Loop depended on animation frames');
    }
    suppressFrames = false;
    send({ control: 'passed', seekPauseResume: true, loop: true, withoutAnimationFrames: true, scheduled });
  } catch (error) { send({ error: String(error) }); }
  app.unmount();
  window.webkit.messageHandlers.probe.postMessage('DONE');
};
</script></body></html>"""


def make_wave(duration=20, frequency=440):
    output = io.BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setparams((2, 2, 44100, 0, 'NONE', 'not compressed'))
        audio.writeframes(b''.join(
            struct.pack('<hh', value, value) for index in range(44100 * duration)
            for value in [int(8000 * math.sin(index * 2 * math.pi * frequency / 44100))]
        ))
    return output.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:1460')
    parser.add_argument('--project', type=Path)
    parser.add_argument('--offset', type=float, default=0)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--ffmpeg', default=shutil.which('ffmpeg'))
    parser.add_argument('--stagger', action='store_true', help='Move tracks to different timeline starts in the fixture')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('Native WKWebView requires macOS')
    if not args.ffmpeg:
        parser.error('FFmpeg is required for preview chunk decoding')
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix='.editor-webkit-smoke-', dir=root) as folder:
        fixture = Path(folder)
        pcm_fixture = fixture / 'continuity.wav'
        pcm_fixture.write_bytes(make_wave(duration=9, frequency=17013.1))
        if args.project:
            project = json.loads(args.project.read_text(encoding='utf-8'))
            for source in project['sources']:
                if not Path(source['path']).is_file():
                    parser.error(f"Missing source: {source['path']}")
                peaks_path = source.get('peaksPath')
                if peaks_path and Path(peaks_path).is_file():
                    peaks = json.loads(Path(peaks_path).read_text(encoding='utf-8'))
                    source['peaks'] = peaks.get('peaks', [])
                    source['channelPeaks'] = peaks.get('channelPeaks', [])
        else:
            tone = fixture / 'tone.wav'
            tone.write_bytes(make_wave())
            project = {
                'id': 'webkit-probe',
                'sources': [{'id': 'tone', 'path': str(tone), 'channels': 2, 'duration': 20,
                             'peaks': [0.25] * 2200, 'channelPeaks': [[0.25] * 2200] * 2}],
                'tracks': [{'id': f'track-{index}', 'sourceId': 'tone', 'volume': 0.1,
                            'pan': 0, 'muted': False, 'solo': False,
                            'clips': [{'id': f'clip-{index}', 'assetId': 'tone', 'start': 0,
                                       'offset': 0, 'duration': 20, 'volume': 1,
                                       'fadeIn': 0, 'fadeOut': 0, 'muted': False}]}
                           for index in range(6)],
            }
        # Normalize mix settings only in this temporary fixture, never on disk.
        for track in project['tracks']:
            track.update(muted=False, solo=False, volume=0.1)
        if args.stagger:
            for index, track in enumerate(project['tracks']):
                for clip in track['clips']:
                    clip['start'] += index * 0.75
        duration = max(clip['start'] + clip['duration']
                       for track in project['tracks'] for clip in track['clips'])
        if not 0 <= args.offset < duration - 11:
            parser.error('Offset must leave at least 11 seconds of playback')
        html = HTML.replace('__PROJECT__', json.dumps(project))
        html = html.replace('__PCM_FIXTURE__', json.dumps(str(pcm_fixture)))
        html = html.replace('__DURATION__', str(duration)).replace('__OFFSET__', str(args.offset))
        (fixture / 'index.html').write_text(html, encoding='utf-8')
        runner = fixture / 'runner'
        swift_source = fixture / 'runner.swift'
        swift_source.write_text(SWIFT_RUNNER, encoding='utf-8')
        subprocess.run(['xcrun', 'swiftc', str(swift_source),
                        '-o', str(runner)], check=True)
        result = subprocess.run([str(runner), f'{args.url}/{fixture.name}/index.html', args.ffmpeg],
                                capture_output=True, text=True, timeout=100)
        if args.output:
            args.output.write_text(result.stdout + result.stderr, encoding='utf-8')
        records = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
        samples = [record for record in records if 'wall' in record]
        failures = [record for record in records if record.get('error')]
        controls = [record for record in records if record.get('control') == 'passed']
        if result.returncode or failures or len(samples) != 25 or not controls:
            raise RuntimeError(result.stdout + result.stderr)
        # Ignore initial output startup latency; compare the subsequent clocks.
        first, last = samples[3], samples[-1]
        elapsed = last['wall'] - first['wall']
        progress = last['time'] - first['time']
        assert last['status'] == 'playing', last
        assert abs(progress - elapsed) < 0.4, (progress, elapsed, last)
        assert any(sample['level'] > 0.001 for sample in samples[3:]), 'Mixer stayed silent'
        assert last['scheduled'] > 0, 'No audio was scheduled'
        assert any(record.get('timing') == 'passed' for record in records), 'Clip timing was not verified'
        assert any(record.get('pcmContinuity') == 'passed' for record in records), 'Output PCM continuity was not verified'
        print(json.dumps({'result': 'passed', 'engine': 'native WKWebView',
                          'startupMilliseconds': next((record['startupMilliseconds'] for record in records
                                                       if 'startupMilliseconds' in record), None),
                          'wallSeconds': elapsed, 'mediaSeconds': progress,
                          'last': last}, ensure_ascii=False))


if __name__ == '__main__':
    main()
