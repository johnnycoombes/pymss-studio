<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { msstOverlapSize, PARAMETER_EXAMPLE, PARAMETER_HELP_COPY, type ParameterHelpTopic } from '@/features/inference/parameterHelp'
import AudioParameterDiagram from '@/components/AudioParameterDiagram.vue'

const props = defineProps<{ topic: ParameterHelpTopic; compact?: boolean; bitRateCodec?: 'mp3' | 'aac' }>()
const preset = defineModel<'video' | 2 | 4>('preset', { default: 'video' })
const { t, locale } = useI18n()
const example = PARAMETER_EXAMPLE
const overlap = computed(() => preset.value === 'video' ? Math.round(example.chunkSize * .2) : msstOverlapSize(example.chunkSize, preset.value)!)
const ratio = computed(() => overlap.value / example.chunkSize)
const overlapStart = computed(() => 80 + 280 * (1 - ratio.value))
const format = (value: number) => new Intl.NumberFormat(locale.value, { maximumFractionDigits: 4 }).format(value)
const seconds = computed(() => format(example.chunkSize / example.sampleRate))
const overlapSeconds = computed(() => format(overlap.value / example.sampleRate))
const title = computed(() => PARAMETER_HELP_COPY[props.topic].title)

function wave(x: number, center: number, width: number, offset: number) {
  return Array.from({ length: Math.floor(width / 6) + 1 }, (_, index) => {
    const position = offset + index
    const height = 5 + 19 * Math.abs(Math.sin(position * .47) * Math.cos(position * .13)) + 11 * Math.abs(Math.sin(position * .083))
    return { x: x + index * 6, top: center - height, bottom: center + height }
  })
}
const chunkWaves = [0, 1, 2].map(index => wave(17 + index * 205, 89, 180, index * 31))
const overlapWaves = computed(() => [
  wave(80, 67, 280, 0),
  wave(overlapStart.value, 136, 280, (1 - ratio.value) * 280 / 6),
])
const spectrum = Array.from({ length: 360 }, (_, index) => ({
  x: 24 + (index % 30) * 20, y: 34 + Math.floor(index / 30) * 9,
  opacity: .12 + .6 * Math.abs(Math.sin(index * .17) * Math.cos(index * .061)),
}))
</script>

<template>
  <div class="parameter-diagram" :class="{ 'parameter-diagram--compact': compact }">
      <AudioParameterDiagram v-if="!['chunk', 'overlap', 'vrWindow', 'vrAggression'].includes(topic)" :topic="topic" :compact="compact" :bit-rate-codec="bitRateCodec" />
      <svg v-else class="parameter-lesson__diagram" viewBox="0 0 636 216" role="img" :aria-label="t(title)">
        <g v-if="topic === 'chunk'">
          <g v-for="(lines, index) in chunkWaves" :key="index" :class="{ 'parameter-lesson__muted-wave': index > 0 }">
            <rect :x="8 + index * 205" y="37" width="198" height="105" rx="8" class="parameter-lesson__block" />
            <line v-for="(line, lineIndex) in lines" :key="lineIndex" :x1="line.x" :x2="line.x" :y1="line.top" :y2="line.bottom" class="parameter-lesson__wave" />
            <path :d="`M${15 + index * 205} 152v5h184v-5`" class="parameter-lesson__bracket" />
            <text :x="107 + index * 205" y="181" class="parameter-lesson__value">{{ t('parameterHelp.seconds', { value: seconds }) }}</text>
          </g>
        </g>
        <g v-else-if="topic === 'overlap'">
          <rect :x="overlapStart" y="26" :width="280 * ratio" height="134" rx="3" class="parameter-lesson__overlap" />
          <line v-for="x in [overlapStart, 360]" :key="x" :x1="x" :x2="x" y1="25" y2="167" class="parameter-lesson__boundary" />
          <g v-for="(lines, index) in overlapWaves" :key="index">
            <text x="4" :y="71 + index * 69" class="parameter-lesson__wave-label">{{ t('parameterHelp.block', { index: index + 1 }) }}</text>
            <line v-for="(line, lineIndex) in lines" :key="lineIndex" :x1="line.x" :x2="line.x" :y1="line.top" :y2="line.bottom" class="parameter-lesson__wave" :class="{ 'parameter-lesson__wave--shared': line.x >= overlapStart && line.x <= 360 }" />
          </g>
          <path :d="`M${overlapStart} 168v5H360v-5`" class="parameter-lesson__bracket" />
          <text :x="(overlapStart + 360) / 2" y="197" class="parameter-lesson__value">{{ t('parameterHelp.overlapSeconds', { value: overlapSeconds }) }}</text>
        </g>
        <g v-else-if="topic === 'vrWindow'">
          <rect v-for="(frame, index) in spectrum" :key="index" :x="frame.x" :y="frame.y" width="13" height="6" rx="1" :opacity="frame.opacity" class="parameter-lesson__spectrum" />
          <rect x="17" y="25" width="195" height="127" rx="5" class="parameter-lesson__window" />
          <path d="M17 164v5h195v-5" class="parameter-lesson__bracket" />
          <text x="114" y="195" class="parameter-lesson__value">{{ t('parameterHelp.spectralFrames') }}</text>
        </g>
        <g v-else>
          <line x1="44" x2="592" y1="96" y2="96" class="parameter-lesson__strength-track" />
          <line x1="44" x2="318" y1="96" y2="96" class="parameter-lesson__strength-fill" />
          <circle cx="318" cy="96" r="8" class="parameter-lesson__spectrum" />
          <text x="44" y="138" class="parameter-lesson__value">0</text>
          <text x="592" y="138" class="parameter-lesson__value">100</text>
          <text x="318" y="181" class="parameter-lesson__value">{{ t('parameterHelp.maskStrength') }}</text>
        </g>
      </svg>
      <div v-if="topic === 'overlap'" class="parameter-lesson__comparison">
        <div role="group" :aria-label="t('parameterHelp.compareOverlap')">
          <button v-for="item in [{ value: 'video' as const, label: '20%' }, { value: 2 as const, label: '50%' }, { value: 4 as const, label: '75%' }]" :key="item.value" type="button" :aria-pressed="preset === item.value" @click="preset = item.value">{{ item.label }}</button>
        </div>
        <p>{{ preset === 'video' ? t('parameterHelp.videoReference') : t('parameterHelp.msstCount', { count: preset }) }}</p>
      </div>
  </div>
</template>

<style scoped>
.parameter-lesson__diagram { display: block; width: 100%; height: auto; margin-top: 31px; overflow: visible; }
.parameter-lesson__block { fill: color-mix(in srgb, var(--primary) 12%, var(--surface-2)); }
.parameter-lesson__wave { stroke: var(--primary); stroke-width: 3; stroke-linecap: round; }
.parameter-lesson__wave--shared { stroke: color-mix(in srgb, var(--primary) 60%, var(--on-surface)); }
.parameter-lesson__muted-wave { opacity: .5; }
.parameter-lesson__bracket { fill: none; stroke: color-mix(in srgb, var(--primary) 70%, transparent); stroke-width: 1; }
.parameter-lesson__value { fill: var(--on-surface-muted); text-anchor: middle; font-size: 13px; }
.parameter-lesson__wave-label { fill: var(--on-surface-muted); font-size: 12px; }
.parameter-lesson__overlap { fill: var(--primary); opacity: .1; }
.parameter-lesson__boundary { stroke: var(--primary); opacity: .5; stroke-dasharray: 3 5; }
.parameter-lesson__spectrum { fill: var(--primary); }
.parameter-lesson__window { fill: color-mix(in srgb, var(--primary) 8%, transparent); stroke: var(--primary); }
.parameter-lesson__strength-track { stroke: var(--outline); stroke-width: 8; stroke-linecap: round; }
.parameter-lesson__strength-fill { stroke: var(--primary); stroke-width: 8; stroke-linecap: round; }
.parameter-lesson__comparison { margin-top: 13px; text-align: center; }
.parameter-lesson__comparison button { min-width: 65px; padding: 5px 13px; border: 1px solid transparent; background: transparent; border-radius: 6px; color: var(--on-surface-muted); cursor: pointer; font: inherit; font-size: 14px; }
.parameter-lesson__comparison button:hover { background: var(--surface-3); }
.parameter-lesson__comparison button[aria-pressed="true"] { color: var(--on-surface); border-color: color-mix(in srgb, var(--primary) 40%, var(--outline)); background: color-mix(in srgb, var(--primary) 12%, var(--surface-2)); }
.parameter-lesson__comparison p { margin: 7px 0 0; font-size: 12px; color: var(--on-surface-muted); }

.parameter-lesson__comparison button:focus-visible { outline: 2px solid var(--primary); outline-offset: 4px; }
.parameter-diagram--compact .parameter-lesson__diagram { margin-top: 0; }
.parameter-diagram--compact .parameter-lesson__diagram text { font-size: 22px; }
.parameter-diagram--compact .parameter-lesson__comparison { margin-top: 6px; }
.parameter-diagram--compact .parameter-lesson__comparison button { min-width: 59px; padding: 4px 10px; font-size: 12px; }
.parameter-diagram--compact .parameter-lesson__comparison p { line-height: 1.6; font-size: 11px; }
@media (max-width: 600px) { .parameter-lesson__diagram text { font-size: 22px; } }
@media (max-height: 700px) { .parameter-diagram:not(.parameter-diagram--compact) .parameter-lesson__diagram { margin-top: 21px; } }
</style>
