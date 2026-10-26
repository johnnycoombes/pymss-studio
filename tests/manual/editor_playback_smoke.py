"""Exercise the fallback media-element transport in Chromium against Vite.

Use a dedicated Vite server with HMR and file watching disabled so that creating
or cleaning up the temporary fixture cannot reload the active playback page.
"""

import argparse
import io
import json
import math
from pathlib import Path
import struct
import tempfile
import wave

from playwright.sync_api import sync_playwright


HTML = """<!doctype html><html><body><div id="app"></div>
<script type="module">
// Select the media fallback; Windows and macOS use scheduled chunks.
Object.defineProperty(navigator, 'platform', { value: 'Linux x86_64' })
import { createApp, reactive, ref, h } from 'vue'
import { createPinia } from 'pinia'
import { useEditorPlayback } from '/src/composables/useEditorPlayback.ts'
const endCase = new URLSearchParams(location.search).has('ended')
const media = []
const NativeAudio = window.Audio
const timeProperty = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'currentTime')
window.Audio = function () {
  const audio = new NativeAudio()
  const index = media.length
  const nativePlay = audio.play.bind(audio)
  audio.playCalls = 0
  audio.play = () => {
    audio.playCalls++
    if (endCase && index === 1 && audio.playCalls === 1) {
      return new Promise((resolve, reject) => setTimeout(() => nativePlay().then(resolve, reject), 90))
    }
    return nativePlay()
  }
  audio.seekCount = 0
  Object.defineProperty(audio, 'currentTime', {
    get() { return timeProperty.get.call(this) },
    set(value) { this.seekCount++; timeProperty.set.call(this, value) },
  })
  media.push(audio)
  return audio
}
const source = { id: 'source', path: new URL('./tone.wav', location.href).pathname, channels: 2, duration: 8 }
const editor = reactive({
  session: { id: 'project', tracks: [0, 1].map(index => ({
    id: `track-${index}`, sourceId: 'source', volume: 0.5, pan: 0, muted: false, solo: false,
    clips: [{ id: `clip-${index}`, assetId: 'source', start: endCase ? 0 : index * 1.5,
      offset: endCase ? 6 - index * 2 : 1 - index * 0.5,
      duration: endCase ? 2 + index * 2 : 4, volume: 1, fadeIn: 0, fadeOut: 0, muted: false }],
  })) },
  sourceMap: new Map([['source', source]]), duration: endCase ? 4 : 5.5, masterVolume: 0.5, masterPan: 0,
})
window.editor = editor
const app = createApp({ setup() {
  const playback = useEditorPlayback({ editor, scrollEl: ref(null) })
  window.playback = playback
  window.sample = () => ({
    time: playback.currentTime.value, status: playback.status.value, level: playback.level.value,
    error: playback.playbackError.value,
    media: media.map(audio => ({ time: audio.currentTime, paused: audio.paused, seeking: audio.seeking,
      readyState: audio.readyState, seeks: audio.seekCount, ended: audio.ended, playCalls: audio.playCalls })),
  })
  return () => h('button', { onClick: () => playback.requestPlay() }, 'Play')
} })
app.use(createPinia()).mount('#app')
window.dispose = () => app.unmount()
</script></body></html>"""


def make_wave():
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(48000)
        frames = bytearray()
        for index in range(8 * 48000):
            sample = int(8000 * math.sin(index * 2 * math.pi * 440 / 48000))
            frames.extend(struct.pack("<hh", sample, sample))
        audio.writeframes(frames)
    return output.getvalue()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:1459")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix=".editor-playback-smoke-", dir=root) as folder:
        fixture = Path(folder)
        (fixture / "index.html").write_text(HTML, encoding="utf-8")
        (fixture / "tone.wav").write_bytes(make_wave())
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                errors = []
                pending_requests = set()
                page.on("request", lambda request: pending_requests.add(request.url))
                page.on("requestfinished", lambda request: pending_requests.discard(request.url))
                page.on("requestfailed", lambda request: pending_requests.discard(request.url))
                page.on("pageerror", lambda error: errors.append(str(error)))
                try:
                    page.goto(f"{args.url}/{fixture.name}/index.html", wait_until="networkidle", timeout=15000)
                except Exception:
                    print(json.dumps({"pending_requests": sorted(pending_requests), "errors": errors}))
                    raise
                assert page.get_by_role("button", name="Play").count() == 1
                page.get_by_role("button", name="Play").click()
                page.wait_for_function("sample().time > 0.5 && sample().level > 0.001")
                first = page.evaluate("sample()")
                assert abs(first["time"] - (first["media"][0]["time"] - 1)) < 0.12, first
                page.wait_for_function("sample().time > 2.2")
                overlap = page.evaluate("sample()")
                assert all(not item["paused"] for item in overlap["media"]), overlap
                assert abs(overlap["media"][0]["time"] - 1 - (overlap["media"][1]["time"] + 1)) < 0.17, overlap
                # Starting a later clip may seek once; steady playback must not keep seeking.
                page.wait_for_timeout(300)
                stable = page.evaluate("sample()")
                assert [item["seeks"] for item in stable["media"]] == [item["seeks"] for item in overlap["media"]], stable
                page.evaluate("""() => {
                    editor.session.tracks[0].volume = 0.3
                    editor.session.tracks[1].pan = -0.4
                }""")
                page.wait_for_timeout(150)
                adjusted = page.evaluate("sample()")
                assert adjusted["time"] > stable["time"], adjusted
                assert [item["playCalls"] for item in adjusted["media"]] == [item["playCalls"] for item in stable["media"]], adjusted
                assert [item["seeks"] for item in adjusted["media"]] == [item["seeks"] for item in stable["media"]], adjusted
                page.evaluate("playback.seek(4.2)")
                page.wait_for_function("sample().status === 'playing' && sample().time > 4.4")
                page.evaluate("playback.requestPause()")
                paused = page.evaluate("sample()")
                page.wait_for_timeout(300)
                assert page.evaluate("sample().time") == paused["time"]
                assert all(item["paused"] for item in paused["media"]), paused
                assert page.evaluate("playback.requestPlay()")
                try:
                    page.wait_for_function("sample().status === 'paused' && sample().time === 0", timeout=7000)
                except Exception:
                    print(json.dumps({"failed_state": page.evaluate("sample()"), "errors": errors}))
                    raise
                page.evaluate("dispose()")
                page.goto(f"{args.url}/{fixture.name}/index.html?ended=1", wait_until="networkidle")
                page.get_by_role("button", name="Play").click()
                page.wait_for_function("sample().time > 2.2")
                ended = page.evaluate("sample()")
                assert ended["media"][0]["ended"], ended
                assert ended["media"][0]["playCalls"] == 1, ended
                assert ended["media"][0]["seeks"] == 1, ended
                assert ended["level"] > 0.001, ended
                page.evaluate("playback.seek(0.2)")
                page.wait_for_function("sample().time > 0.3 && sample().time < 1")
                assert not page.evaluate("sample().media[0].paused")
                page.evaluate("dispose()")
                assert not errors, errors
                print(json.dumps({"result": "passed", "engine": "Chromium", "overlap": overlap, "paused": paused, "ended": ended}, ensure_ascii=False))
            finally:
                browser.close()


if __name__ == "__main__":
    main()
