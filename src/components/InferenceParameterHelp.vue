<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import InferenceParameterLesson from '@/components/InferenceParameterLesson.vue'
import { parameterHelpTopics } from '@/features/inference/parameterHelp'

const props = defineProps<{ show: boolean; modelType?: string | null }>()
const emit = defineEmits<{ 'update:show': [value: boolean] }>()
const { t } = useI18n()
const page = ref(0)
const topics = computed(() => parameterHelpTopics(props.modelType))
watch(() => props.show, () => { page.value = 0 })
watch(topics, () => { page.value = 0 })
const close = () => emit('update:show', false)
</script>

<template>
  <n-modal :show="show" :mask-closable="true" @update:show="emit('update:show', $event)">
    <InferenceParameterLesson
      :topic="topics[page]!" :topics="topics" :progress="`${page + 1} / ${topics.length}`" :can-go-back="page > 0"
      :next-label="page === topics.length - 1 ? t('parameterHelp.done') : undefined"
      @select="topic => page = topics.indexOf(topic)"
      @next="page < topics.length - 1 ? page++ : close()" @previous="page--" @close="close"
    />
  </n-modal>
</template>
