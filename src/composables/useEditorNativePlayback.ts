import { computed, onBeforeUnmount, watch } from 'vue'
import i18n from '@/i18n'
import type { useEditorStore } from '@/stores/editor'
import { useEditorPlaybackStore } from '@/stores/editorPlayback'
import { hasEditorPlugins, pluginProjectSignature } from '@/utils/editorPlugins'
import { ensurePluginSession, PluginHostDialogPendingError, pluginHostCapabilities, pluginHostRequest, type PluginHostStatus } from '@/utils/pluginHost'

export function useEditorNativePlayback(editor: ReturnType<typeof useEditorStore>, followPlayhead: () => void) {
  const playback = useEditorPlaybackStore()
  const required = computed(() => hasEditorPlugins(editor.session))
  let activeProjectId: string | null = null
  let failedProjectId: string | null = null
  let generation = 0
  let mixGeneration = 0
  let disposed = false
  let polling = false
  let pollingEnabled = false
  let timer: ReturnType<typeof setTimeout> | null = null

  function clearPoll() {
    if (timer) clearTimeout(timer)
    timer = null
  }

  function stopPoll() {
    pollingEnabled = false
    clearPoll()
  }

  function fail(id: number, cause: unknown) {
    if (cause instanceof PluginHostDialogPendingError) {
      if (!disposed && playback.requestId === id && activeProjectId) {
        pollingEnabled = true
        schedulePoll()
      } else if (!disposed && playback.requestId === id) playback.finishPause(id)
      return
    }
    stopPoll()
    if (!disposed && playback.requestId === id) failedProjectId = editor.session?.id || null
    playback.fail(id, cause instanceof Error ? cause.message : String(cause))
  }

  function recordingOwnsPosition(projectId: string) {
    return playback.recordingProjectId === projectId
  }

  async function syncMonitor(projectId: string, serial: number) {
    if (disposed || serial !== generation || activeProjectId !== projectId || editor.session?.id !== projectId) return false
    const capabilities = await pluginHostCapabilities()
    if (disposed || serial !== generation || activeProjectId !== projectId || editor.session?.id !== projectId) return false
    const suppressedTrackIds = [...playback.suppressedTrackIds]
    if (capabilities.recording !== true) {
      if (suppressedTrackIds.length) throw new Error(String(i18n.global.t('editor.plugins.recordingBlocked')))
      return true
    }
    await pluginHostRequest({ op: 'monitor', projectId, suppressedTrackIds })
    return !disposed && serial === generation && activeProjectId === projectId && editor.session?.id === projectId
  }

  async function ensureLatestProject(project: NonNullable<typeof editor.session>, serial: number) {
    const signature = pluginProjectSignature(project)
    try { await ensurePluginSession(project) }
    catch (cause) {
      // An edit can repair the chain while the old load is still outstanding.
      if (disposed || serial !== generation || editor.session !== project
        || signature === pluginProjectSignature(project) || !hasEditorPlugins(project)) throw cause
      await ensurePluginSession(project)
    }
  }

  function applyStatus(result: PluginHostStatus, id: number, projectId: string, serial: number, preservePosition = false) {
    if (disposed || generation !== serial || playback.requestId !== id || editor.session?.id !== projectId) return
    if (result.projectId && result.projectId !== projectId) return
    if (result.error) { fail(id, new Error(result.error)); return }
    failedProjectId = null
    playback.clearError()
    const recording = recordingOwnsPosition(projectId)
    if (!recording && !preservePosition) playback.setCurrentTime(result.position)
    playback.setMasterLevel(result.masterLevel || [0, 0])
    playback.setTrackLevels(result.levels || {})
    const master = result.masterLevel || [0, 0]
    playback.setLevel((master[0] + master[1]) / 2)
    if (result.playing || (recording && playback.intent === 'play')) playback.finishPlay(id)
    else playback.finishPause(id)
    followPlayhead()
  }

  function schedulePoll() {
    clearPoll()
    if (disposed || !activeProjectId || !pollingEnabled) return
    timer = setTimeout(async () => {
      if (polling || !activeProjectId || disposed) return
      polling = true
      const projectId = activeProjectId
      const serial = generation
      const id = playback.requestId
      const mix = mixGeneration
      try {
        const result = await pluginHostRequest<PluginHostStatus>({ op: 'status', projectId })
        if (mix === mixGeneration && failedProjectId !== projectId) applyStatus(result, id, projectId, serial)
      } catch (cause) {
        if (serial === generation && projectId === activeProjectId && mix === mixGeneration && failedProjectId !== projectId) fail(id, cause)
      } finally {
        polling = false
        if (pollingEnabled && playback.status !== 'error') schedulePoll()
      }
    }, 100)
  }

  async function play(position = playback.currentTime) {
    const project = editor.session
    if (!project || disposed) return false
    const serial = ++generation
    stopPoll()
    const id = playback.beginRequest('play', 'starting')
    try {
      await ensureLatestProject(project, serial)
      if (serial !== generation || editor.session?.id !== project.id || disposed) return false
      activeProjectId = project.id
      if (!await syncMonitor(project.id, serial)) return false
      const result = await pluginHostRequest<PluginHostStatus>({ op: 'play', projectId: project.id, position, loop: recordingOwnsPosition(project.id) ? false : playback.loop })
      if (serial !== generation || disposed || editor.session?.id !== project.id) return false
      applyStatus(result, id, project.id, serial)
      pollingEnabled = playback.status !== 'error'
      schedulePoll()
      return playback.status === 'playing'
    } catch (cause) {
      if (serial === generation && !disposed) fail(id, cause)
      return false
    }
  }

  function pause(reset = false) {
    const preservePosition = playback.recordingProjectId !== null
      && playback.recordingProjectId === (activeProjectId || editor.session?.id)
    if (disposed) {
      const id = playback.beginRequest('pause', 'paused')
      if (reset && !preservePosition) playback.setCurrentTime(0)
      playback.finishPause(id)
      return true
    }
    const projectId = activeProjectId
    const serial = ++generation
    const id = playback.beginRequest('pause', 'pausing')
    stopPoll()
    if (!projectId) {
      if (reset && !preservePosition) playback.setCurrentTime(0)
      playback.finishPause(id)
      return true
    }
    void (async () => {
      try {
        let result = await pluginHostRequest<PluginHostStatus>({ op: 'pause', projectId })
        if (reset && serial === generation) result = await pluginHostRequest<PluginHostStatus>({ op: 'seek', projectId, position: 0 })
        if (!required.value && serial === generation && !disposed && editor.session?.id === projectId) {
          if (playback.finishPause(id)) playback.clearError()
          return
        }
        applyStatus(result, id, projectId, serial, preservePosition)
        if (serial === generation && !disposed && !preservePosition) {
          pollingEnabled = playback.status !== 'error'
          schedulePoll()
        }
      } catch (cause) {
        if (serial === generation && !disposed) {
          if (!required.value && editor.session?.id === projectId) {
            if (playback.finishPause(id)) playback.clearError()
          } else fail(id, cause)
        }
      }
    })()
    return true
  }

  function seek(position: number) {
    const project = editor.session
    if (!project) return
    const serial = ++generation
    stopPoll()
    const playing = playback.intent === 'play'
    const id = playback.beginRequest(playing ? 'play' : 'pause', playing ? 'starting' : 'paused')
    playback.setCurrentTime(position)
    void (async () => {
      try {
        await ensureLatestProject(project, serial)
        if (serial !== generation || disposed || editor.session?.id !== project.id) return
        activeProjectId = project.id
        if (!await syncMonitor(project.id, serial)) return
        const result = await pluginHostRequest<PluginHostStatus>({ op: 'seek', projectId: project.id, position })
        if (serial !== generation || disposed || editor.session?.id !== project.id) return
        applyStatus(result, id, project.id, serial)
        pollingEnabled = playback.status !== 'error'
        schedulePoll()
      } catch (cause) {
        if (serial === generation && !disposed) fail(id, cause)
      }
    })()
  }

  watch(() => editor.session && pluginProjectSignature(editor.session), async () => {
    const update = ++mixGeneration
    const project = editor.session
    if (!project || (project.id !== activeProjectId && project.id !== failedProjectId) || !hasEditorPlugins(project)) return
    const serial = generation
    const id = playback.requestId
    const current = () => !disposed && update === mixGeneration && serial === generation && editor.session === project && playback.requestId === id
    try {
      await ensurePluginSession(project)
      if (!current()) return
      const recovering = failedProjectId === project.id
      if (recovering) activeProjectId = project.id
      if (!await syncMonitor(project.id, serial) || !current()) return
      if (recovering) {
        const result = await pluginHostRequest<PluginHostStatus>({ op: 'status', projectId: project.id })
        if (!current()) return
        applyStatus(result, id, project.id, serial)
        pollingEnabled = playback.status !== 'error'
        schedulePoll()
      }
    } catch (cause) {
      if (current()) {
        void pluginHostRequest({ op: 'pause', projectId: project.id }).catch(() => undefined)
        fail(id, cause)
      }
    }
  })
  watch(() => playback.suppressedTrackIds, async () => {
    const projectId = activeProjectId
    if (!projectId || editor.session?.id !== projectId || disposed) return
    const serial = generation
    try { await syncMonitor(projectId, serial) }
    catch (cause) { if (serial === generation && !disposed) fail(playback.requestId, cause) }
  })
  watch(() => playback.loop, loop => {
    if (activeProjectId && playback.intent === 'play' && !recordingOwnsPosition(activeProjectId)) void play(playback.currentTime)
  })
  watch(() => editor.session, (project, oldProject) => {
    if (project?.id === oldProject?.id) return
    generation += 1
    stopPoll()
    activeProjectId = null
    failedProjectId = null
    if (oldProject) void (async () => {
      await editor.releasePluginSession(oldProject).catch(() => undefined)
    })()
  })
  watch(required, (value, previous) => {
    if (!value && previous) {
      pause()
      activeProjectId = null
      failedProjectId = null
      playback.clearError()
      const projectId = editor.session?.id
      if (projectId && editor.session) void editor.releasePluginSession(editor.session).catch(() => undefined)
    }
  })
  onBeforeUnmount(() => {
    disposed = true
    generation += 1
    stopPoll()
    const project = editor.session
    activeProjectId = null
    if (project) void (async () => {
      await pluginHostRequest({ op: 'pause', projectId: project.id }).catch(() => undefined)
      await editor.releasePluginSession(project).catch(() => undefined)
    })()
  })

  return { required, play, pause, seek }
}
