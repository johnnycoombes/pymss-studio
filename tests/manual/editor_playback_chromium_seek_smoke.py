"""Measure Windows preview seeks and rendered PCM in Chromium or Edge.

Run directly; a temporary bundle and local HTTP server are managed here:
    python tests/manual/editor_playback_chromium_seek_smoke.py
    ... --channel msedge

The real playback modules, AudioContext and FFmpeg run on this host. HTTP
stands in for Tauri IPC, so this does not measure WebView2 or device latency.
"""

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
from threading import Thread
from urllib.parse import parse_qs, urlparse
import wave

from playwright.sync_api import sync_playwright


HTML = """<!doctype html><html><body><div id=app></div><script type=module>
import {createApp, reactive, ref, h, createPinia, useEditorPlayback} from './playback.js';
Object.defineProperty(navigator, 'platform', {value: 'Win32'});
const NativeAudio = window.Audio;
let mediaElements = 0, ctx, analyser;
window.Audio = function() { mediaElements++; return new NativeAudio(); };
const NativeContext = window.AudioContext;
window.AudioContext = function() {
  ctx = new NativeContext();
  const createGain = ctx.createGain.bind(ctx);
  let first = true;
  ctx.createGain = () => {
    const gain = createGain();
    if (first) {
      first = false; analyser = ctx.createAnalyser();
      analyser.fftSize = 8192; analyser.smoothingTimeConstant = 0;
      gain.connect(analyser);
    }
    return gain;
  };
  return ctx;
};
window.__TAURI_INTERNALS__ = {
  convertFileSrc: path => path,
  invoke: async (command, args) => {
    if (command === 'debug_editor_playback') return;
    if (command !== 'editor_preview_chunk') throw Error(command);
    const response = await fetch('/preview?' + new URLSearchParams(args));
    if (!response.ok) throw Error(await response.text());
    return response.arrayBuffer();
  },
};
const sources = [44100, 48000, 96000, 44100, 48000, 96000].map((rate, index) => ({
  id: String(index), path: 'tone-' + index + '.wav', channels: 2, sampleRate: rate, duration: 24,
}));
const editor = reactive({
  session: {id: 'project', tracks: Array.from({length: 6}, (_, i) => ({
    id: 'track-' + i, sourceId: sources[i].id, volume: 1/6, pan: 0, muted: false, solo: false,
    clips: [{id: 'clip-' + i, assetId: sources[i].id, start: 0, offset: 0,
      duration: 24, volume: 1, fadeIn: 0, fadeOut: 0, muted: false}],
  }))},
  sourceMap: new Map(sources.map(source => [source.id, source])),
  duration: 24, masterVolume: .5, masterPan: 0,
});
const app = createApp({setup() {
  const playback = useEditorPlayback({editor, scrollEl: ref(null)});
  window.playback = playback;
  window.sample = () => {
    const data = new Float32Array(analyser.frequencyBinCount);
    analyser.getFloatFrequencyData(data);
    let peak = 1;
    for (let i = 2; i < data.length; i++) if (data[i] > data[peak]) peak = i;
    return {time: playback.currentTime.value, status: playback.status.value,
      contextTime: ctx.currentTime, frequency: peak * ctx.sampleRate / analyser.fftSize,
      db: data[peak], error: playback.playbackError.value, mediaElements};
  };
  window.seekAndMeasure = async target => {
    const start = performance.now();
    playback.seek(target);
    // Require actual output at the requested position, not only a moved playhead.
    for (;;) {
      await new Promise(resolve => setTimeout(resolve, 10));
      const state = sample();
      if (state.error) throw Error(state.error);
      if (state.status === 'playing' && state.db > -60
          && Math.abs((state.frequency - 300)/100 - state.time) < .18) {
        return {latencyMs: performance.now() - start, ...state};
      }
      if (performance.now() - start > 5000) throw Error(JSON.stringify(state));
    }
  };
  return () => h('button', {onClick: () => playback.requestPlay()}, 'Play');
}});
app.use(createPinia()).mount('#app');
window.dispose = () => app.unmount();
</script></body></html>"""


def make_wave(path, rate):
    # Frequency encodes source position: 300 + 100 * project_seconds.
    frames = bytearray()
    for index in range(24 * rate):
        time = index / rate
        sample = round(10000 * math.sin(2 * math.pi * (300 * time + 50 * time * time)))
        frames.extend(struct.pack('<hh', sample, sample))
    with wave.open(str(path), 'wb') as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(frames)


def bundle(root, fixture):
    (fixture / 'entry.ts').write_text(
        "export {createApp, reactive, ref, h} from 'vue';\n"
        "export {createPinia} from 'pinia';\n"
        "export {useEditorPlayback} from '../src/composables/useEditorPlayback';\n",
        encoding='utf-8',
    )
    (fixture / 'i18n.ts').write_text(
        'export default {global: {t: (key: string) => key}}', encoding='utf-8',
    )
    # esbuild is already supplied by Vite; no additional dependencies are installed.
    script = """
const {createRequire} = require('node:module');
const [root, fixture] = process.argv.slice(1);
const {buildSync} = createRequire(require.resolve('vite'))('esbuild');
buildSync({entryPoints: [fixture + '/entry.ts'], outfile: fixture + '/playback.js',
  bundle: true, format: 'esm', platform: 'browser',
  alias: {'@/i18n': fixture + '/i18n.ts', '@': root + '/src'}});
"""
    subprocess.run(['node', '-e', script, root.as_posix(), fixture.as_posix()], check=True, cwd=root)


def handler_for(fixture, ffmpeg):
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            url = urlparse(self.path)
            if url.path != '/preview':
                return super().do_GET()
            try:
                args = parse_qs(url.query)
                name = args['path'][0]
                if name not in {f'tone-{index}.wav' for index in range(6)}:
                    raise ValueError('Unknown fixture')
                index, rate, channels = (int(args[key][0]) for key in ('index', 'sampleRate', 'channels'))
                start = index * 4
                warm = max(0, start - 1)
                # Mirror decode_editor_preview_chunk, including sample-aligned resampling.
                command = [ffmpeg, '-nostdin', '-v', 'error', '-threads', '1', '-copyts', '-start_at_zero']
                if start:
                    command += ['-ss', str(warm)]
                command += ['-i', str(fixture / name), '-map', '0:a:0', '-vn', '-af',
                            f'atrim=start={warm},asetpts=PTS-{warm}/TB,aresample={rate}:first_pts=0,'
                            f'atrim=start={start - warm}:duration=4,asetpts=PTS-STARTPTS',
                            '-t', '4', '-ar', str(rate), '-ac', str(channels),
                            '-c:a', 'pcm_f32le', '-f', 'f32le', 'pipe:1']
                result = subprocess.run(command, capture_output=True, check=True, timeout=15)
                data = result.stdout
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as error:
                self.send_error(500, str(error))

    return partial(Handler, directory=str(fixture))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--channel', default=None, help='Use msedge to test installed Microsoft Edge')
    parser.add_argument('--ffmpeg', default=shutil.which('ffmpeg'))
    args = parser.parse_args()
    if not args.ffmpeg:
        parser.error('FFmpeg is required; use --ffmpeg to select the executable')
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix='.editor-chromium-seek-', dir=root) as folder:
        fixture = Path(folder)
        (fixture / 'index.html').write_text(HTML, encoding='utf-8')
        for index, rate in enumerate((44100, 48000, 96000, 44100, 48000, 96000)):
            make_wave(fixture / f'tone-{index}.wav', rate)
        bundle(root, fixture)
        with ThreadingHTTPServer(('127.0.0.1', 0), handler_for(fixture, args.ffmpeg)) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True, channel=args.channel)
                    try:
                        page = browser.new_page()
                        errors = []
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        page.goto(f'http://127.0.0.1:{server.server_port}/index.html', wait_until='networkidle')
                        page.get_by_role('button', name='Play').click()
                        page.wait_for_function('sample().time > .5 && sample().db > -60')
                        assert page.evaluate('sample().mediaElements') == 0
                        seeks = [page.evaluate('target => seekAndMeasure(target)', target) for target in (12, 3, 18)]
                        assert max(state['latencyMs'] for state in seeks) < 1500, seeks
                        page.evaluate('''async () => {
                            for (const target of [2, 17, 1, 13, 6, .3, .7, .1]) {
                                playback.seek(target);
                                await new Promise(resolve => setTimeout(resolve, 20));
                            }
                        }''')
                        latest = page.evaluate('target => seekAndMeasure(target)', .4)
                        assert latest['latencyMs'] < 500, latest
                        page.wait_for_timeout(250)
                        before = page.evaluate('sample()')
                        page.wait_for_timeout(600)
                        after = page.evaluate('sample()')
                        assert abs(after['time'] - before['time'] - (after['contextTime'] - before['contextTime'])) < .06
                        assert abs((after['frequency'] - 300)/100 - after['time']) < .18, after
                        # Audio frequency must also advance at normal speed after scrubbing.
                        assert abs((after['frequency'] - before['frequency'])/100
                                   - (after['contextTime'] - before['contextTime'])) < .18, (before, after)
                        page.evaluate('playback.requestPause()')
                        paused = page.evaluate('sample().time')
                        page.wait_for_timeout(150)
                        assert page.evaluate('sample().time') == paused
                        page.evaluate('dispose()')
                        assert not errors, errors
                        print(json.dumps({'result': 'passed', 'engine': args.channel or 'Chromium',
                                          'tracks': 6, 'sourceRates': [44100, 48000, 96000],
                                          'coldSeekMs': [round(state['latencyMs']) for state in seeks],
                                          'cachedSeekMs': round(latest['latencyMs']),
                                          'clockDelta': round(after['time'] - before['time'], 3)}, ensure_ascii=False))
                    finally:
                        browser.close()
            finally:
                server.shutdown()
                thread.join()


if __name__ == '__main__':
    main()
