// Stereo-linked sample-peak limiter; mirror native/audio-host/src/limiter.rs.
export class MasterLimiterDSP {
  constructor(rate, config) {
    this.rate = rate
    this.delay = [new Float32Array(0), new Float32Array(0)]
    this.peaks = new Float64Array(0)
    this.configure(config)
  }

  configure(config = {}) {
    this.enabled = config.enabled === true
    const bounded = (value, fallback, min, max) => Number.isFinite(value) ? Math.max(min, Math.min(max, value)) : fallback
    const attackMs = bounded(config.attackMs, 3, 0.1, 100)
    const releaseMs = bounded(config.releaseMs, 250, 5, 2000)
    this.latency = this.enabled ? Math.ceil(attackMs * this.rate / 1000) : 0
    if (this.delay[0].length !== this.latency || !this.peaks.length) {
      this.delay = [new Float32Array(this.latency), new Float32Array(this.latency)]
      this.peaks = new Float64Array(this.latency + 2)
      this.indices = new Float64Array(this.latency + 2)
      this.reset()
    }
    this.gain = 10 ** (bounded(config.gainDb, 0, 0, 30) / 20)
    this.ceiling = 10 ** (bounded(config.ceilingDb, 0, -30, 0) / 20)
    this.attack = Math.exp(-Math.log(1000) / Math.max(1, this.latency))
    this.release = Math.exp(-1 / (releaseMs * this.rate / 1000))
  }

  reset() {
    this.delay[0].fill(0)
    this.delay[1].fill(0)
    this.cursor = this.head = this.count = this.index = 0
    this.envelope = 1
  }

  process(left, right, outLeft, outRight, frames = outLeft.length) {
    for (let i = 0; i < frames; i++) {
      const l = Number.isFinite(left?.[i]) ? left[i] : 0
      const r = Number.isFinite(right?.[i]) ? right[i] : 0
      if (!this.enabled) { outLeft[i] = l; outRight[i] = r; continue }
      const peak = Math.max(Math.abs(l), Math.abs(r))
      const capacity = this.peaks.length
      while (this.count > 0 && this.indices[this.head] + this.latency < this.index) {
        this.head = (this.head + 1) % capacity
        this.count--
      }
      while (this.count > 0 && this.peaks[(this.head + this.count - 1) % capacity] <= peak) this.count--
      const tail = (this.head + this.count) % capacity
      this.peaks[tail] = peak
      this.indices[tail] = this.index++
      this.count++
      const maximum = this.peaks[this.head] * this.gain
      const target = maximum > this.ceiling ? this.ceiling / maximum : 1
      const coefficient = target < this.envelope ? this.attack : this.release
      this.envelope = target + coefficient * (this.envelope - target)
      const delayedLeft = this.delay[0][this.cursor]
      const delayedRight = this.delay[1][this.cursor]
      this.delay[0][this.cursor] = l
      this.delay[1][this.cursor] = r
      this.cursor = (this.cursor + 1) % this.latency
      const delayedPeak = Math.max(Math.abs(delayedLeft), Math.abs(delayedRight)) * this.gain
      const safe = delayedPeak > this.ceiling ? this.ceiling / delayedPeak : 1
      const gain = this.gain * Math.min(this.envelope, safe)
      outLeft[i] = delayedLeft * gain
      outRight[i] = delayedRight * gain
    }
  }
}

if (typeof AudioWorkletProcessor !== 'undefined' && typeof registerProcessor === 'function') {
  registerProcessor('pymss-master-limiter', class extends AudioWorkletProcessor {
    constructor(options) {
      super()
      this.limiter = new MasterLimiterDSP(sampleRate, options.processorOptions)
      this.port.onmessage = ({ data }) => {
        if (data.type === 'reset') this.limiter.reset()
        else if (data.type === 'configure') this.limiter.configure(data.config)
      }
    }

    process(inputs, outputs) {
      const output = outputs[0]
      if (output?.length >= 2) this.limiter.process(inputs[0]?.[0], inputs[0]?.[1] ?? inputs[0]?.[0], output[0], output[1])
      return true
    }
  })
}
