<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { PARAMETER_HELP_COPY, type ParameterHelpTopic } from '@/features/inference/parameterHelp'

const props = defineProps<{ topic: ParameterHelpTopic; compact?: boolean; bitRateCodec?: 'mp3' | 'aac' }>()
const { t, locale } = useI18n()
const augmented = ref(true)
const batchSize = ref(1)
const bitRate = ref(320)
const selectedCodec = ref<'mp3' | 'aac'>('mp3')
const codec = computed(() => props.bitRateCodec || selectedCodec.value)
const bitRates = computed(() => codec.value === 'aac' ? [128, 192, 320, 512] : [128, 192, 320])
const maxBitRate = computed(() => bitRates.value.at(-1)!)
const bitRateBarY = (index: number) => bitRates.value.length === 4 ? 49 + index * 40 : 52 + index * 57
const bitDepthOptions = [
  { label: 'audio.pcm16', description: 'parameterHelp.bitDepthPcm16' },
  { label: 'audio.pcm24', description: 'parameterHelp.bitDepthPcm24' },
  { label: 'audio.float', description: 'parameterHelp.bitDepthFloat' },
]
const isTta = computed(() => props.topic === 'tta' || props.topic === 'vrTta')
const passes = computed(() => augmented.value ? (props.topic === 'vrTta' ? 2 : 3) : 1)
const variants = computed(() => {
  const keys = !augmented.value ? ['parameterHelp.diagram.original'] : props.topic === 'vrTta'
    ? ['parameterHelp.diagram.aligned', 'parameterHelp.diagram.shifted']
    : ['parameterHelp.diagram.original', 'parameterHelp.diagram.swap', 'parameterHelp.diagram.invert']
  return keys.map((key, index) => ({ key, y: keys.length === 1 ? 111 : keys.length === 2 ? 73 + index * 76 : 43 + index * 68 }))
})
const rawSignal = Array.from({ length: 97 }, (_, index) => Math.sin(index / 96 * Math.PI * 4) + .25 * Math.sin(index / 96 * Math.PI * 14))
const peak = Math.max(...rawSignal.map(Math.abs))
const signal = rawSignal.map(value => value / peak)
function wave(x: number, y: number, width: number, amplitude: number, bias = 0, peakLimit?: number) {
  return signal.map((value, index) => {
    const offset = value * amplitude
    const sample = peakLimit === undefined ? offset : Math.max(-peakLimit, Math.min(peakLimit, offset))
    const px = x + index / (signal.length - 1) * width
    const py = y - bias - sample
    return index === 0 ? `M${px},${py}` : `L${px},${py}`
  }).join(' ')
}
const fileSize = computed(() => new Intl.NumberFormat(locale.value, { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(bitRate.value * 60 / 8000))
watch(() => props.topic, () => { augmented.value = true; batchSize.value = 1; bitRate.value = 320; selectedCodec.value = 'mp3' })
watch(codec, () => { bitRate.value = 320 })
</script>

<template>
  <div class="audio-diagram" :class="{ 'audio-diagram--compact': compact }" :data-diagram="topic">
    <div v-if="topic === 'bitRate' && !bitRateCodec" class="diagram-options bit-rate-codecs" role="group" :aria-label="t('parameterHelp.diagram.chooseCodec')">
      <button v-for="value in ['mp3', 'aac'] as const" :key="value" type="button" :aria-pressed="codec === value" @click="selectedCodec = value">{{ value.toUpperCase() }}</button>
    </div>
    <p v-if="topic === 'bitRate'" class="bit-rate-limit">{{ t(codec === 'mp3' ? 'parameterHelp.mp3BitRateLimit' : 'parameterHelp.aacBitRateLimit') }}</p>
    <dl v-if="topic === 'bitDepth'" class="bit-depth-options">
      <div v-for="option in bitDepthOptions" :key="option.label">
        <dt>{{ t(option.label) }}</dt><dd>{{ t(option.description) }}</dd>
      </div>
    </dl>
    <svg viewBox="0 0 636 242" role="img" :aria-label="t(PARAMETER_HELP_COPY[topic].title)">
      <g v-if="isTta">
        <g v-for="variant in variants" :key="variant.key">
          <rect x="10" :y="variant.y - 24" width="162" height="48" rx="8" class="panel" />
          <text x="91" :y="variant.y + 6">{{ t(variant.key) }}</text>
          <path :d="`M179,${variant.y}H229m-7,-5l7,5l-7,5`" class="arrow" />
          <rect x="240" :y="variant.y - 24" width="140" height="48" rx="8" class="panel" />
          <text x="310" :y="variant.y + 6">{{ t('parameterHelp.diagram.inference') }}</text>
          <path :d="`M390,${variant.y}H425L482,111`" class="arrow" />
        </g>
        <rect x="490" y="76" width="136" height="70" rx="8" class="panel panel--accent" />
        <text x="558" y="103">{{ t(augmented ? (topic === 'vrTta' ? 'parameterHelp.diagram.align' : 'parameterHelp.diagram.restore') : 'parameterHelp.diagram.output') }}</text>
        <text v-if="augmented" x="558" y="130">{{ t('parameterHelp.diagram.average') }}</text>
        <text x="318" y="232" class="caption">{{ t('parameterHelp.diagram.passes', { count: passes }) }}</text>
      </g>
      <g v-else-if="topic === 'standardize'">
        <text x="142" y="25">{{ t('parameterHelp.diagram.input') }}</text>
        <text x="496" y="25">{{ t('parameterHelp.diagram.standardized') }}</text>
        <rect x="14" y="44" width="254" height="130" rx="8" class="panel" />
        <rect x="370" y="44" width="254" height="130" rx="8" class="panel" />
        <line x1="24" x2="258" y1="109" y2="109" class="axis" />
        <line x1="380" x2="614" y1="109" y2="109" class="axis" />
        <path :d="wave(28, 109, 226, 23, 8)" class="wave" />
        <path :d="wave(384, 109, 226, 47)" class="wave" />
        <path d="M283,82h69m-7,-5l7,5l-7,5" class="arrow" />
        <text x="318" y="115" class="formula">(x − μ) / σ</text>
        <text x="142" y="199" class="caption">{{ t('parameterHelp.diagram.offset') }}</text>
        <text x="496" y="199" class="caption">{{ t('parameterHelp.diagram.zeroMean') }}</text>
        <text x="318" y="235" class="caption">{{ t('parameterHelp.diagram.restoreScale') }}</text>
      </g>
      <g v-else-if="topic === 'normalize'">
        <text x="146" y="24">{{ t('parameterHelp.diagram.before') }}</text>
        <text x="492" y="24">{{ t('parameterHelp.diagram.after') }}</text>
        <text x="318" y="73" class="caption">0 dBFS</text>
        <line x1="24" x2="614" y1="82" y2="82" class="peak-limit" />
        <line x1="24" x2="614" y1="178" y2="178" class="peak-limit" />
        <line x1="24" x2="272" y1="130" y2="130" class="axis" />
        <line x1="370" x2="614" y1="130" y2="130" class="axis" />
        <path :d="wave(26, 130, 244, 65)" class="wave" />
        <path :d="wave(374, 130, 238, 42)" class="wave" />
        <path d="M289,130h61m-7,-5l7,5l-7,5" class="arrow" />
        <text x="318" y="235" class="caption">{{ t('parameterHelp.diagram.attenuatePeak') }}</text>
      </g>
      <g v-else-if="topic === 'batch'">
        <text x="164" y="31">{{ t('parameterHelp.diagram.chunks') }}</text>
        <rect v-for="index in 8" :key="index" :x="20 + (index - 1) % 4 * 74" :y="60 + Math.floor((index - 1) / 4) * 65" width="60" height="48" rx="7" class="batch-block" :class="{ 'batch-block--active': index <= batchSize }" />
        <path d="M322,113h48m-7,-5l7,5l-7,5" class="arrow" />
        <rect x="393" y="60" width="220" height="113" rx="10" class="panel panel--accent" />
        <text x="503" y="102">{{ t('parameterHelp.diagram.model') }}</text>
        <text x="503" y="141">{{ t('parameterHelp.diagram.batchCount', { count: batchSize }) }}</text>
        <text x="318" y="225" class="caption">{{ t('parameterHelp.diagram.batchCost') }}</text>
      </g>
      <g v-else-if="topic === 'bitDepth'">
        <text x="151" y="26">PCM_16 / PCM_24</text>
        <text x="489" y="26">WAV FLOAT</text>
        <g v-for="offset in [0, 338]" :key="offset">
          <rect :x="14 + offset" y="44" width="274" height="154" rx="8" class="panel" />
          <line :x1="24 + offset" :x2="278 + offset" y1="80" y2="80" class="peak-limit" />
          <line :x1="24 + offset" :x2="278 + offset" y1="164" y2="164" class="peak-limit" />
          <line :x1="24 + offset" :x2="278 + offset" y1="122" y2="122" class="axis" />
        </g>
        <path :d="wave(24, 122, 254, 64)" class="reference" />
        <path :d="wave(24, 122, 254, 64, 0, 42)" class="wave" />
        <path :d="wave(362, 122, 254, 64)" class="wave" />
        <text x="151" y="225" class="caption">{{ t('parameterHelp.diagram.clippedPeak') }}</text>
        <text x="489" y="225" class="caption">{{ t('parameterHelp.diagram.retainedPeak') }}</text>
      </g>
      <g v-else-if="topic === 'bitRate'">
        <text x="318" y="31">{{ t('parameterHelp.diagram.oneMinute') }}</text>
        <g v-for="(rate, index) in bitRates" :key="rate" :opacity="rate === bitRate ? 1 : .4">
          <text x="88" :y="bitRateBarY(index) + 24">{{ rate }} kbps</text>
          <rect x="172" :y="bitRateBarY(index)" :width="rate / maxBitRate * 400" :height="bitRates.length === 4 ? 28 : 34" rx="5" class="rate-bar" />
        </g>
        <text x="318" y="234" class="caption">{{ t('parameterHelp.diagram.fileSize', { size: fileSize }) }}</text>
      </g>
    </svg>
    <p v-if="topic === 'bitDepth'" class="bit-depth-note">{{ t('parameterHelp.bitDepthNote') }}</p>
    <div v-if="isTta" class="diagram-options" role="group" :aria-label="t('parameterHelp.diagram.compareTta')">
      <button v-for="value in [false, true]" :key="String(value)" type="button" :aria-pressed="augmented === value" @click="augmented = value">{{ t(value ? 'parameterHelp.diagram.enabled' : 'parameterHelp.diagram.disabled') }}</button>
    </div>
    <div v-else-if="topic === 'batch'" class="diagram-options" role="group" :aria-label="t('parameterHelp.batchTitle')">
      <button v-for="value in [1, 4]" :key="value" type="button" :aria-pressed="batchSize === value" @click="batchSize = value">{{ t('parameterHelp.diagram.batchCount', { count: value }) }}</button>
    </div>
    <div v-else-if="topic === 'bitRate'" class="diagram-options bit-rate-options" role="group" :aria-label="t('parameterHelp.bitRateTitle')">
      <button v-for="value in bitRates" :key="value" type="button" :aria-pressed="bitRate === value" @click="bitRate = value">{{ value }} kbps</button>
    </div>
  </div>
</template>

<style scoped>
svg { display: block; width: 100%; height: auto; margin-top: 28px; overflow: visible; }
text { fill: var(--on-surface); text-anchor: middle; font-size: 16px; }
.caption { fill: var(--on-surface-muted); font-size: 14px; }
.panel { fill: color-mix(in srgb, var(--primary) 5%, var(--surface-2)); stroke: var(--outline); }
.panel--accent { fill: color-mix(in srgb, var(--primary) 12%, var(--surface-2)); stroke: var(--primary-border); }
.arrow { fill: none; stroke: var(--on-surface-muted); stroke-width: 1.5; stroke-linejoin: round; }
.axis { stroke: var(--outline-strong); stroke-dasharray: 3 5; }
.peak-limit { stroke: var(--on-surface-muted); stroke-dasharray: 6 5; stroke-opacity: .65; }
.audio-diagram[data-diagram='standardize'] .axis { stroke: var(--on-surface-muted); stroke-opacity: .55; }
.wave, .reference { fill: none; stroke: var(--primary); stroke-width: 2.5; stroke-linecap: round; stroke-linejoin: round; }
.reference { stroke: var(--on-surface-muted); opacity: .3; stroke-width: 1.5; }
.batch-block { fill: var(--surface-3); stroke: var(--outline); }
.batch-block--active, .rate-bar { fill: var(--primary); opacity: .75; }
.bit-depth-options { margin: 16px 0 0; font-size: 13px; line-height: 1.65; }
.bit-depth-options > div { display: grid; grid-template-columns: 72px minmax(0, 1fr); gap: 12px; padding: 8px 0; border-top: 1px solid var(--outline); }
.bit-depth-options dt { color: var(--on-surface); font-weight: 600; }
.bit-depth-options dd { margin: 0; color: var(--on-surface-muted); }
.bit-depth-note { margin: 10px 0 0; color: var(--on-surface-muted); font-size: 12px; line-height: 1.65; }
.bit-rate-limit { margin: 10px 0 0; color: var(--on-surface-muted); font-size: 13px; line-height: 1.65; text-align: center; }
.diagram-options { display: flex; align-items: center; justify-content: center; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.diagram-options button { border: 1px solid transparent; border-radius: 6px; padding: 5px 12px; background: transparent; color: var(--on-surface-muted); font: inherit; font-size: 13px; cursor: pointer; }
.diagram-options button:hover { background: var(--surface-3); }
.diagram-options button[aria-pressed='true'] { color: var(--on-surface); border-color: var(--primary-border); background: color-mix(in srgb, var(--primary) 10%, var(--surface-2)); }
.diagram-options button:focus-visible { outline: 2px solid var(--primary); outline-offset: 3px; }
.audio-diagram--compact svg { margin-top: 0; }
.audio-diagram--compact text { font-size: 22px; }
.audio-diagram--compact .caption { font-size: 20px; }
.audio-diagram--compact .formula { font-size: 19px; }
.audio-diagram--compact .bit-depth-options { margin-top: 12px; font-size: 12px; }
.audio-diagram--compact .bit-depth-options > div { grid-template-columns: 62px minmax(0, 1fr); gap: 8px; padding: 6px 0; }
.audio-diagram--compact .bit-depth-note { font-size: 11px; }
.audio-diagram--compact .bit-rate-limit { margin-top: 0; font-size: 12px; }
.audio-diagram--compact .diagram-options button { font-size: 12px; padding: 4px 8px; }
@media (max-width: 600px) { text { font-size: 21px; } .caption { font-size: 19px; } .formula { font-size: 18px; } }
</style>
