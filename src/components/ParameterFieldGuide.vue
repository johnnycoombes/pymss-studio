<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useBeginnerGuideStore } from '@/stores/beginnerGuide'
import type { ParameterGuide } from '@/features/onboarding/parameters'
import InferenceParameterDiagram from '@/components/InferenceParameterDiagram.vue'

const props = defineProps<{ parameter: ParameterGuide; target: HTMLElement | null }>()
const { t, locale } = useI18n()
const guide = useBeginnerGuideStore()
const targetReady = computed(() => props.target?.getAttribute('data-parameter-guide') === props.parameter.id)
const popover = ref<{ syncPosition: () => void } | null>(null)
const popup = ref<HTMLElement | null>(null)
const diagramPreset = ref<'video' | 2 | 4>('video')
const verticalOffset = ref(0)
const horizontalOffset = ref(0)
const popupWidth = ref(310)
const popupMaxHeight = ref(160)
const dialog = ref<HTMLElement | null>(null)
const placement = ref<'left' | 'right' | 'left-start' | 'right-start' | 'left-end' | 'right-end' | 'top' | 'bottom'>('right')
const bounds = ref({ width: 1, height: 1 })
const highlight = ref<{ x: number; y: number; width: number; height: number } | null>(null)
const titleId = useId()
const maskId = useId()
let frame = 0
let disposed = false
let resizeObserver: ResizeObserver | null = null
let observed = new Set<HTMLElement>()

async function syncPosition() {
  await nextTick()
  if (disposed) return
  const root = props.target?.closest<HTMLElement>('.settings-modal')
  dialog.value = root || null
  if (!root || !props.target) { highlight.value = null; return }
  if (resizeObserver) {
    const next = new Set([root, props.target, ...(popup.value ? [popup.value] : [])])
    for (const element of observed) if (!next.has(element)) resizeObserver.unobserve(element)
    for (const element of next) if (!observed.has(element)) resizeObserver.observe(element)
    observed = next
  }
  const field = props.target.getBoundingClientRect()
  const rect = root.getBoundingClientRect()
  const content = props.target.closest('.n-card__content, .n-card-content')?.getBoundingClientRect() || rect
  const scaleX = rect.width / root.offsetWidth || 1
  const scaleY = rect.height / root.offsetHeight || 1
  const left = Math.max(field.left - 5, rect.left, content.left)
  const top = Math.max(field.top - 5, rect.top, content.top)
  const right = Math.min(field.right + 5, rect.right, content.right)
  const bottom = Math.min(field.bottom + 5, rect.bottom, content.bottom)
  bounds.value = { width: root.clientWidth, height: root.clientHeight }
  highlight.value = right > left && bottom > top ? {
    x: (left - rect.left) / scaleX - root.clientLeft,
    y: (top - rect.top) / scaleY - root.clientTop,
    width: (right - left) / scaleX, height: (bottom - top) / scaleY,
  } : null
  const inset = 16 * scaleX
  const leftSpace = field.left - rect.left - inset - 16 * scaleX
  const rightSpace = rect.right - field.right - inset - 16 * scaleX
  const sideSpace = Math.max(leftSpace, rightSpace)
  const desiredWidth = props.parameter.diagram ? 380 : 310
  popupWidth.value = Math.min(desiredWidth, (sideSpace >= 280 * scaleX ? sideSpace : rect.width - 2 * inset) / scaleX)
  if (sideSpace < 280 * scaleX) {
    const above = (field.top - rect.top) / scaleY - 32
    const below = (rect.bottom - field.bottom) / scaleY - 32
    placement.value = below > above ? 'bottom' : 'top'
    popupMaxHeight.value = Math.max(120, Math.max(above, below))
  } else {
    popupMaxHeight.value = Math.max(160, root.clientHeight - 32)
    await nextTick()
    const height = popup.value?.getBoundingClientRect().height || 210
    const side = leftSpace > rightSpace ? 'left' : 'right'
    const center = field.top + field.height / 2
    placement.value = center - height / 2 < rect.top + 8 ? `${side}-start`
      : center + height / 2 > rect.bottom - 8 ? `${side}-end` : side
  }
  await nextTick()
  if (disposed) return
  popover.value?.syncPosition()
  await nextTick()
  if (disposed || !popup.value) return
  const popupRect = popup.value.getBoundingClientRect()
  const naturalTop = popupRect.top - verticalOffset.value * scaleY
  const naturalLeft = popupRect.left - horizontalOffset.value * scaleX
  const leftLimit = rect.left + inset
  const rightLimit = rect.right - popupRect.width - inset
  horizontalOffset.value = (Math.max(leftLimit, Math.min(naturalLeft, rightLimit)) - naturalLeft) / scaleX
  const topLimit = rect.top + 16 * scaleY
  const bottomLimit = rect.bottom - popupRect.height - 16 * scaleY
  verticalOffset.value = (Math.max(topLimit, Math.min(naturalTop, bottomLimit)) - naturalTop) / scaleY
}
function schedulePosition() {
  if (!frame && !disposed) frame = requestAnimationFrame(() => { frame = 0; void syncPosition() })
}
function handleResize() {
  if (targetReady.value) props.target?.scrollIntoView({ block: 'nearest', behavior: 'instant' })
  schedulePosition()
}
watch([() => props.target, () => props.parameter.id, locale], () => { void syncPosition() }, { flush: 'post', immediate: true })
watch(() => props.parameter.id, () => { diagramPreset.value = 'video'; verticalOffset.value = 0; horizontalOffset.value = 0 })
onMounted(() => {
  resizeObserver = new ResizeObserver(schedulePosition)
  window.addEventListener('resize', handleResize)
  window.addEventListener('scroll', schedulePosition, true)
  void syncPosition()
})
onBeforeUnmount(() => {
  disposed = true
  if (frame) cancelAnimationFrame(frame)
  resizeObserver?.disconnect()
  observed.clear()
  window.removeEventListener('resize', handleResize)
  window.removeEventListener('scroll', schedulePosition, true)
})
defineExpose({ syncPosition })
</script>

<template>
  <Teleport v-if="dialog" :to="dialog">
    <svg class="parameter-field-guide-mask" :viewBox="`0 0 ${bounds.width} ${bounds.height}`" preserveAspectRatio="none" aria-hidden="true">
      <defs><mask :id="maskId" x="0" y="0" :width="bounds.width" :height="bounds.height" maskUnits="userSpaceOnUse"><rect :width="bounds.width" :height="bounds.height" fill="white" /><rect v-if="highlight && targetReady" class="parameter-field-guide-cutout" :x="highlight.x" :y="highlight.y" :width="highlight.width" :height="highlight.height" rx="7" fill="black" /></mask></defs>
      <rect :width="bounds.width" :height="bounds.height" fill="black" fill-opacity=".42" :mask="`url(#${maskId})`" />
      <rect v-if="highlight && targetReady" class="parameter-field-guide-highlight" :x="highlight.x" :y="highlight.y" :width="highlight.width" :height="highlight.height" rx="7" fill="none" stroke="var(--primary)" stroke-width="1.5" />
    </svg>
  </Teleport>
  <Teleport v-if="target" :to="target">
    <n-popover ref="popover" trigger="manual" :show="targetReady" :placement="placement" :to="dialog || false" :animated="false" :show-arrow="false" :theme-overrides="{ space: '16px' }" raw>
      <template #trigger><span class="parameter-field-guide__anchor" aria-hidden="true" /></template>
      <section ref="popup" class="parameter-field-guide guide-coach" :style="{ transform: `translate(${horizontalOffset}px, ${verticalOffset}px)`, width: popupWidth + 'px', maxHeight: popupMaxHeight + 'px' }" :data-parameter="parameter.id" role="region" :aria-labelledby="titleId">
        <div class="guide-coach__head"><span>{{ t(guide.chapterTitle) }} <span class="guide-coach__count">{{ guide.progress }}</span></span><button type="button" class="guide-coach__skip" @click="guide.end('skipped')">{{ t('beginnerGuide.skip') }}</button></div>
        <div class="parameter-field-guide__body">
          <h3 :id="titleId">{{ t(parameter.title) }}</h3>
          <p aria-live="polite">{{ t(parameter.description) }}</p>
          <div v-if="parameter.diagram" class="parameter-field-guide__diagram">
            <InferenceParameterDiagram :topic="parameter.diagram" :bit-rate-codec="parameter.id === 'mp3BitRate' ? 'mp3' : parameter.id === 'm4aBitRate' ? 'aac' : undefined" compact v-model:preset="diagramPreset" />
            <p class="parameter-field-guide__example">{{ t('parameterHelp.diagramExample') }}</p>
          </div>
        </div>
        <footer class="guide-coach__footer">
          <div class="guide-coach__actions"><n-button text size="small" :disabled="guide.navigating" @click="guide.move(-1)">{{ t('common.back') }}</n-button><n-button type="primary" size="small" :disabled="guide.navigating" @click="guide.move(1)">{{ t('common.next') }}</n-button></div>
        </footer>
      </section>
    </n-popover>
  </Teleport>
</template>

<style scoped>
.parameter-field-guide.guide-coach { display: flex; flex-direction: column; overflow: hidden; }
.parameter-field-guide .guide-coach__head,
.parameter-field-guide .guide-coach__footer { flex: 0 0 auto; }
.parameter-field-guide__body { min-height: 0; overflow-y: auto; overscroll-behavior: contain; }
.parameter-field-guide__anchor { position: absolute; inset: 0; pointer-events: none; }
.parameter-field-guide-mask { position: absolute; inset: 0; z-index: 1; width: 100%; height: 100%; pointer-events: none; }
.parameter-field-guide__diagram { min-width: 0; margin-top: 16px; }
.parameter-field-guide .parameter-field-guide__example { margin-top: 10px; font-size: 11px; line-height: 1.6; }
</style>

<style scoped src="./beginner-guide-coach.css"></style>
