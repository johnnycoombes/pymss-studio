import assert from 'node:assert/strict'
import test from 'node:test'
import { DEFAULT_MASTER_LIMITER, masterLimiterRenderRate, normalizeMasterLimiter } from '../src/utils/masterLimiter.ts'
import { MasterLimiterDSP } from '../src/audio/masterLimiterProcessor.js'

function render(input: number[], config: object, rate = 8000, block = 127) {
  const dsp = new MasterLimiterDSP(rate, { ...DEFAULT_MASTER_LIMITER, enabled: true, ...config })
  const left = Float32Array.from([...input, ...new Array(dsp.latency).fill(0)])
  const right = Float32Array.from(left, value => -value * 0.25)
  const l = new Float32Array(left.length), r = new Float32Array(left.length)
  for (let offset = 0; offset < left.length; offset += block) {
    dsp.process(left.subarray(offset, offset + block), right.subarray(offset, offset + block),
      l.subarray(offset, offset + block), r.subarray(offset, offset + block))
  }
  return { dsp, left: l, right: r }
}

test('old projects default to bypass; finite parameters are bounded without accepting truthy enable values', () => {
  assert.deepEqual(normalizeMasterLimiter(), DEFAULT_MASTER_LIMITER)
  assert.deepEqual(normalizeMasterLimiter({ enabled: 'yes', gainDb: Infinity, ceilingDb: -40, attackMs: 0, releaseMs: 5000 } as never),
    { enabled: false, gainDb: 0, ceilingDb: -30, attackMs: 0.1, releaseMs: 2000 })
})

test('disabled limiter is exact stereo passthrough, including peaks over unity', () => {
  const result = render([0.1, 2, -3, 0.25], { enabled: false, gainDb: 30, ceilingDb: -30 })
  assert.equal(result.dsp.latency, 0)
  assert.deepEqual(result.left, Float32Array.from([0.1, 2, -3, 0.25]))
  assert.deepEqual(result.right, Float32Array.from([-0.025, -0.5, 0.75, -0.0625]))
})

test('mixed-bus peaks are limited after gain while the stereo ratio and last frame survive delay compensation', () => {
  const input = Array.from({ length: 5000 }, (_, i) => i % 137 === 0 || i === 4999 ? -4 : Math.sin(i * 0.04) * 0.1)
  for (const rate of [8000, 44100, 48000, 96000]) {
    const { dsp, left, right } = render(input, { gainDb: 30, ceilingDb: -12, attackMs: 3 }, rate)
    const ceiling = 10 ** (-12 / 20)
    assert.equal(dsp.latency, Math.ceil(rate * 0.003))
    assert.ok(left.subarray(0, dsp.latency).every(value => value === 0))
    assert.ok(left.every(value => Math.abs(value) <= ceiling + 1e-7))
    left.forEach((value, i) => assert.ok(Math.abs(right[i] + value * 0.25) < 1e-7))
    assert.ok(Math.abs(left.at(-1)!) > 0, 'the final transient is retained when trimming lookahead')
  }
})

test('attack and release both affect the gain envelope; render chunk size does not', () => {
  const input = Array.from({ length: 5000 }, (_, i) => i === 300 ? 10 : 0.1)
  const fast = render(input, { attackMs: 1, releaseMs: 5 })
  const slow = render(input, { attackMs: 30, releaseMs: 2000 })
  assert.ok(slow.left[slow.dsp.latency + 280] < fast.left[fast.dsp.latency + 280], 'longer attack anticipates the peak earlier')
  assert.ok(slow.left[slow.dsp.latency + 1000] < fast.left[fast.dsp.latency + 1000], 'longer release recovers later')
  assert.deepEqual(render(input, { attackMs: 30 }, 8000, 1).left, render(input, { attackMs: 30 }, 8000, 1024).left)
})

test('reset and bypass discard old delayed audio and gain reduction', () => {
  const dsp = new MasterLimiterDSP(8000, { ...DEFAULT_MASTER_LIMITER, enabled: true })
  dsp.process(new Float32Array(100).fill(10), new Float32Array(100).fill(5), new Float32Array(100), new Float32Array(100))
  dsp.reset()
  const output = new Float32Array(100)
  dsp.process(new Float32Array(100), new Float32Array(100), output, new Float32Array(100))
  assert.ok(output.every(value => value === 0))
  dsp.configure({ ...DEFAULT_MASTER_LIMITER, enabled: false })
  dsp.process(Float32Array.of(2), Float32Array.of(1), output, new Float32Array(100), 1)
  assert.equal(output[0], 2)
})

test('limiter render rate follows encoding and only audible source rates for automatic exports', () => {
  const project = { sources: [
    { id: 'voice', sampleRate: 44100 }, { id: 'muted', sampleRate: 96000 },
  ], tracks: [
    { sourceId: 'voice', muted: false }, { sourceId: 'muted', muted: true },
  ] } as never
  assert.equal(masterLimiterRenderRate(project), 44100)
  assert.equal(masterLimiterRenderRate(project, 32000), 32000)
  assert.equal(masterLimiterRenderRate({ sources: [], tracks: [] } as never), 48000)
})
