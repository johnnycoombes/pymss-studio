import { computed, ref } from 'vue'
import { acceptHMRUpdate, defineStore } from 'pinia'
import { useSettingsStore } from '@/stores/settings'
import { BEGINNER_GUIDE_STEPS, BEGINNER_GUIDE_CHAPTERS, normalizeBeginnerGuideProgress } from '@/features/onboarding/guide'
import { isMainTaskOwner } from '@/utils/taskOwner'
import type { ParameterGuide } from '@/features/onboarding/parameters'

export const useBeginnerGuideStore = defineStore('beginnerGuide', () => {
  const settings = useSettingsStore()
  const phase = ref<'closed' | 'invitation' | 'running' | 'paused'>('closed')
  const pauseReason = ref<'page' | 'runtime' | 'mode'>('page')
  const navigationRequest = ref(0)
  const navigating = ref(false)
  const visible = computed(() => phase.value !== 'closed')
  const demoActive = computed(() => phase.value === 'running' || phase.value === 'paused')
  const macOS = ref(false)
  const steps = computed(() => BEGINNER_GUIDE_STEPS.filter(item => !macOS.value || !item.hiddenOnMac))
  const currentStep = computed(() => {
    const saved = normalizeBeginnerGuideProgress(settings.beginnerGuideProgress).step
    return steps.value.find(item => item === BEGINNER_GUIDE_STEPS[saved])
      || steps.value.find(item => BEGINNER_GUIDE_STEPS.indexOf(item) > saved) || steps.value.at(-1)!
  })
  const step = computed(() => BEGINNER_GUIDE_STEPS.indexOf(currentStep.value))
  const chapterTitle = computed(() => BEGINNER_GUIDE_CHAPTERS[currentStep.value.chapter])
  const hasNextChapter = computed(() => steps.value.some(item => BEGINNER_GUIDE_STEPS.indexOf(item) > step.value && item.chapter !== currentStep.value.chapter))
  const parameterStep = computed(() => phase.value === 'running' && Boolean(currentStep.value.parameters))
  const parameterSteps = ref<ParameterGuide[]>([])
  const groupParameters = computed(() => parameterSteps.value.filter(item => item.group === currentStep.value.parameters))
  const currentParameter = computed(() => groupParameters.value.find(item => item.id === settings.beginnerGuideProgress.parameter) || groupParameters.value[0])
  const progress = computed(() => {
    const count = (item: typeof currentStep.value) => item.parameters
      ? Math.max(1, parameterSteps.value.filter(parameter => parameter.group === item.parameters).length) : 1
    const index = steps.value.indexOf(currentStep.value)
    const before = steps.value.slice(0, index).reduce((total, item) => total + count(item), 0)
    const current = before + 1 + (parameterStep.value ? Math.max(0, groupParameters.value.findIndex(item => item.id === currentParameter.value?.id)) : 0)
    return `${current} / ${steps.value.reduce((total, item) => total + count(item), 0)}`
  })

  function saveStep(index: number, parameter?: ParameterGuide['id']) {
    settings.beginnerGuideProgress = { status: 'active', step: index, stepId: BEGINNER_GUIDE_STEPS[index]!.id, ...(parameter ? { parameter } : {}) }
  }

  function setPlatform(isMac: boolean) {
    if (macOS.value === isMac) return
    const previous = currentStep.value.id
    macOS.value = isMac
    if (phase.value === 'running' && currentStep.value.id !== previous) {
      saveStep(step.value)
      navigationRequest.value += 1
      navigating.value = true
    }
  }

  function offer() {
    if (!isMainTaskOwner() || phase.value !== 'closed' || settings.beginnerGuideInvitationSeen) return
    settings.beginnerGuideInvitationSeen = true
    phase.value = 'invitation'
  }

  function start(restart = false) {
    if (!isMainTaskOwner()) return
    settings.beginnerGuideInvitationSeen = true
    restart = restart || settings.beginnerGuideProgress.status !== 'active'
    saveStep(restart ? 0 : step.value, restart ? undefined : settings.beginnerGuideProgress.parameter)
    phase.value = 'running'
    navigating.value = true
    navigationRequest.value += 1
  }

  function move(delta: number) {
    if (phase.value !== 'running' || navigating.value) return
    if (parameterStep.value && currentParameter.value && Math.abs(delta) === 1) {
      const index = groupParameters.value.findIndex(item => item.id === currentParameter.value?.id) + delta
      const next = groupParameters.value[index]
      if (next) {
        saveStep(step.value, next.id)
        navigationRequest.value += 1
        navigating.value = true
        return
      }
    }
    const next = steps.value[Math.max(0, Math.min(steps.value.length - 1, steps.value.indexOf(currentStep.value) + delta))]!
    const nextGroup = parameterSteps.value.filter(item => item.group === next.parameters)
    const parameter = next.parameters ? (delta < 0 ? nextGroup.at(-1) : nextGroup[0])?.id : undefined
    saveStep(BEGINNER_GUIDE_STEPS.indexOf(next), parameter)
    navigationRequest.value += 1
    navigating.value = true
  }

  function skipChapter() {
    if (phase.value !== 'running' || navigating.value) return
    const next = steps.value.findIndex(item => BEGINNER_GUIDE_STEPS.indexOf(item) > step.value && item.chapter !== currentStep.value.chapter)
    if (next >= 0) {
      saveStep(BEGINNER_GUIDE_STEPS.indexOf(steps.value[next]!))
      navigationRequest.value += 1
      navigating.value = true
    }
  }

  function goTo(id: string) {
    if (phase.value !== 'running' || navigating.value) return
    const next = steps.value.find(item => item.id === id)
    if (!next) return
    const parameter = next.parameters ? parameterSteps.value.find(item => item.group === next.parameters)?.id : undefined
    saveStep(BEGINNER_GUIDE_STEPS.indexOf(next), parameter)
    navigationRequest.value += 1
    navigating.value = true
  }

  function registerParameters(items: ParameterGuide[]) {
    const previous = groupParameters.value.findIndex(item => item.id === currentParameter.value?.id)
    parameterSteps.value = items
    if (parameterStep.value && !groupParameters.value.some(item => item.id === settings.beginnerGuideProgress.parameter)) {
      const next = groupParameters.value[Math.max(0, Math.min(previous, groupParameters.value.length - 1))]
      if (next) {
        saveStep(step.value, next.id)
        navigationRequest.value += 1
        navigating.value = true
      }
    }
  }

  function pause(reason: 'page' | 'runtime' | 'mode' = 'page') {
    if (phase.value !== 'running') return
    pauseReason.value = reason
    navigating.value = false
    phase.value = 'paused'
  }

  function end(status: 'skipped' | 'completed') {
    if (!isMainTaskOwner()) return
    settings.beginnerGuideInvitationSeen = true
    settings.beginnerGuideProgress = { ...settings.beginnerGuideProgress, status }
    phase.value = 'closed'
    navigating.value = false
  }

  return { phase, pauseReason, navigationRequest, navigating, visible, demoActive, step, steps, currentStep, chapterTitle, hasNextChapter, parameterStep, parameterSteps, currentParameter, progress, setPlatform, skipChapter, goTo, registerParameters, offer, start, move, pause, end }
})

if (import.meta.hot) import.meta.hot.accept(acceptHMRUpdate(useBeginnerGuideStore, import.meta.hot))
