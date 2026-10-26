<script setup lang="ts">
import { computed, ref, useId, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ArrowForwardOutline, ChevronDownOutline } from '@vicons/ionicons5'
import InferenceParameterDiagram from '@/components/InferenceParameterDiagram.vue'
import { msstOverlapSize, PARAMETER_EXAMPLE, PARAMETER_HELP_COPY, type ParameterHelpTopic } from '@/features/inference/parameterHelp'

const props = withDefaults(defineProps<{
  topic: ParameterHelpTopic
  progress: string
  eyebrow?: string
  nextLabel?: string
  closeLabel?: string
  canGoBack?: boolean
  topics?: ParameterHelpTopic[]
}>(), { canGoBack: true })
const emit = defineEmits<{ next: []; previous: []; close: []; select: [topic: ParameterHelpTopic] }>()
const { t, locale } = useI18n()
const titleId = useId()
const detailsId = useId()
const detailsVisible = ref(false)
const preset = ref<'video' | 2 | 4>('video')
const example = PARAMETER_EXAMPLE
const overlap = computed(() => preset.value === 'video'
  ? Math.round(example.chunkSize * .2)
  : msstOverlapSize(example.chunkSize, preset.value)!)
const format = (value: number) => new Intl.NumberFormat(locale.value, { maximumFractionDigits: 4 }).format(value)
const seconds = computed(() => format(example.chunkSize / example.sampleRate))
const overlapSeconds = computed(() => format(overlap.value / example.sampleRate))
const copy = computed(() => PARAMETER_HELP_COPY[props.topic])
const topicOptions = computed(() => props.topics?.map(topic => ({ key: topic, label: t(PARAMETER_HELP_COPY[topic].title) })) || [])
const isVr = computed(() => props.topic === 'vrWindow' || props.topic === 'vrAggression')
const isChunkTopic = computed(() => props.topic === 'chunk' || props.topic === 'overlap')
function selectTopic(value: string | number) {
  const topic = String(value) as ParameterHelpTopic
  if (props.topics?.includes(topic)) emit('select', topic)
}

watch(() => props.topic, () => { detailsVisible.value = false })
</script>

<template>
  <section class="parameter-lesson" role="dialog" aria-modal="true" :aria-labelledby="titleId">
    <div class="parameter-lesson__body">
      <div class="parameter-lesson__topline">
        <n-dropdown v-if="topicOptions.length" trigger="click" :options="topicOptions" @select="selectTopic">
          <button type="button" class="parameter-lesson__text parameter-lesson__topic-menu" aria-haspopup="menu">{{ t('parameterHelp.chooseTopic') }}<n-icon :component="ChevronDownOutline" size="13" /><span class="parameter-lesson__count">{{ progress }}</span></button>
        </n-dropdown>
        <span v-else>{{ eyebrow || t('parameterHelp.label') }} <span class="parameter-lesson__count">{{ progress }}</span></span>
        <button type="button" class="parameter-lesson__text" @click="$emit('close')">{{ closeLabel || t('common.close') }}</button>
      </div>
      <div aria-live="polite" aria-atomic="true">
        <h2 :id="titleId">{{ t(copy.title) }}</h2>
        <p class="parameter-lesson__description">{{ t(copy.description) }}</p>
      </div>
      <InferenceParameterDiagram :topic="topic" v-model:preset="preset" />
      <p class="parameter-lesson__takeaway">{{ t(copy.takeaway) }}</p>
      <section v-if="detailsVisible" :id="detailsId" class="parameter-lesson__details">
        <p>{{ t(copy.detail) }}</p>
        <p v-if="isChunkTopic" class="parameter-lesson__formula">{{ t('parameterHelp.sampleCalculation', { chunk: format(example.chunkSize), rate: format(example.sampleRate), seconds }) }}</p>
        <p v-if="topic === 'overlap'" class="parameter-lesson__formula">{{ t('parameterHelp.overlapCalculation', { overlap: format(overlap), seconds: overlapSeconds, hop: format((example.chunkSize - overlap) / example.sampleRate) }) }}</p>
        <p v-if="topic === 'overlap' && preset !== 'video'">{{ t('parameterHelp.msstFormula', { count: preset }) }}</p>
        <p v-if="isChunkTopic">{{ t('parameterHelp.exampleOnly') }}</p>
        <details v-if="isChunkTopic || isVr">
          <summary>{{ t('parameterHelp.otherParameters') }}</summary>
          <dl>
            <dt>{{ t('inference.batchSize') }}</dt><dd>{{ t('parameterHelp.batchDetail') }}</dd>
            <template v-if="!isVr"><dt>{{ t('separate.tta') }}</dt><dd>{{ t('parameterHelp.ttaDetail') }}</dd><dt>{{ t('inference.standardize') }}</dt><dd>{{ t('parameterHelp.standardizeDetail') }}</dd></template>
            <dt>{{ t('inference.normalize') }}</dt><dd>{{ t('parameterHelp.normalizeDetail') }}</dd>
          </dl>
        </details>
      </section>
    </div>
    <footer class="parameter-lesson__footer">
      <button type="button" class="parameter-lesson__text" :aria-expanded="detailsVisible" :aria-controls="detailsId" @click="detailsVisible = !detailsVisible">{{ t(detailsVisible ? 'parameterHelp.hideDetails' : 'parameterHelp.details') }}</button>
      <div class="parameter-lesson__actions">
        <n-button v-if="canGoBack" text size="small" @click="$emit('previous')">{{ t('common.back') }}</n-button>
        <n-button type="primary" size="small" @click="$emit('next')">{{ nextLabel || t('common.next') }}<template v-if="!nextLabel" #icon><n-icon :component="ArrowForwardOutline" /></template></n-button>
      </div>
    </footer>
  </section>
</template>

<style scoped>
.parameter-lesson { display: flex; flex-direction: column; width: var(--modal-width-md); max-height: var(--modal-max-height); overflow: hidden; background: var(--surface-2); color: var(--on-surface); border: 1px solid var(--outline); border-radius: 16px; box-sizing: border-box; }
.parameter-lesson__body { min-height: 0; overflow-y: auto; overscroll-behavior: contain; scrollbar-gutter: stable; scrollbar-width: thin; scrollbar-color: color-mix(in srgb, var(--on-surface-muted) 35%, transparent) transparent; padding: 28px 42px 32px; }
@supports selector(::-webkit-scrollbar) {
  .parameter-lesson__body { scrollbar-width: auto; scrollbar-color: auto; }
}
.parameter-lesson__body::-webkit-scrollbar { width: 6px; }
.parameter-lesson__body::-webkit-scrollbar-track { background: transparent; margin-block: 16px; }
.parameter-lesson__body::-webkit-scrollbar-thumb { border-radius: 999px; background: color-mix(in srgb, var(--on-surface-muted) 35%, transparent); }
.parameter-lesson__body::-webkit-scrollbar-thumb:hover { background: color-mix(in srgb, var(--on-surface-muted) 55%, transparent); }
.parameter-lesson__body::-webkit-scrollbar-button,
.parameter-lesson__body::-webkit-scrollbar-corner { display: none; width: 0; height: 0; }
.parameter-lesson__topline { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 30px; font-size: 12px; color: var(--on-surface-muted); }
.parameter-lesson__count { margin-left: 10px; font-variant-numeric: tabular-nums; }
.parameter-lesson h2 { margin: 0; font-size: 26px; line-height: 1.35; font-weight: 600; }
.parameter-lesson__description { margin: 10px 0 0; color: var(--on-surface-muted); font-size: 14px; }
.parameter-lesson__takeaway { margin: 26px 0 0; text-align: center; color: var(--on-surface-muted); font-size: 13px; line-height: 1.8; text-wrap: pretty; }
.parameter-lesson__text { padding: 4px 0; border: 0; background: transparent; color: var(--on-surface-muted); cursor: pointer; font: inherit; font-size: 12px; }
.parameter-lesson__text:hover { color: var(--on-surface); }
.parameter-lesson__text:focus-visible, summary:focus-visible { outline: 2px solid var(--primary); outline-offset: 4px; }
.parameter-lesson__topic-menu { display: inline-flex; align-items: center; gap: 6px; }
.parameter-lesson__details { margin-top: 28px; padding-top: 20px; border-top: 1px solid var(--outline); font-size: 13px; line-height: 1.8; color: var(--on-surface-muted); }
.parameter-lesson__details p { margin: 0 0 12px; }
.parameter-lesson__formula { font-variant-numeric: tabular-nums; color: var(--on-surface); }
.parameter-lesson__details summary { cursor: pointer; width: fit-content; }
.parameter-lesson__details dl { display: grid; grid-template-columns: 90px minmax(0, 1fr); gap: 12px 14px; margin: 16px 0 0; }
.parameter-lesson__details dt { color: var(--on-surface); }
.parameter-lesson__details dd { margin: 0; }
.parameter-lesson__footer { display: flex; flex: 0 0 auto; align-items: center; justify-content: space-between; gap: 20px; border-top: 1px solid var(--outline); padding: 18px 42px; }
.parameter-lesson__actions { display: flex; align-items: center; gap: 22px; }
@media (max-width: 600px) { .parameter-lesson__body { padding: 23px 24px 27px; } .parameter-lesson h2 { font-size: 24px; } .parameter-lesson__description { font-size: 13px; } .parameter-lesson__footer { padding: 16px 24px; } .parameter-lesson__actions { gap: 15px; } }
@media (max-height: 700px) { .parameter-lesson__body { padding-top: 21px; padding-bottom: 22px; } .parameter-lesson__topline { margin-bottom: 20px; } .parameter-lesson__takeaway { margin-top: 18px; } .parameter-lesson__footer { padding-top: 14px; padding-bottom: 14px; } }
</style>
