import { computed, onMounted, onScopeDispose, ref, watch, type Ref } from 'vue'

type Corner = 'nw' | 'ne' | 'sw' | 'se'
type Size = { width: number; height: number }
const STORAGE_KEY = 'pymss-plugin-manager-window-v1'
const clamp = (value: number, min: number, max: number) => Math.min(Math.max(value, min), max)

export function usePluginManagerWindow(open: Ref<boolean>, card: Ref<{ $el: HTMLElement } | null>) {
  const viewport = ref({ width: window.innerWidth, height: window.innerHeight })
  const preferred = ref<Size | null>(null)
  const resizingWindow = ref<Corner | null>(null)
  const topInset = computed(() => viewport.value.height <= 640 ? 80 : 96)
  const minWidth = computed(() => Math.min(680, viewport.value.width))
  const minHeight = computed(() => Math.min(480, viewport.value.height))
  const windowSize = computed(() => {
    const { width, height } = viewport.value
    const inset = width <= 760 ? 12 : width <= 880 ? 16 : 24
    return {
      width: clamp(preferred.value?.width ?? Math.min(1440, width - inset * 2), minWidth.value, width),
      height: clamp(preferred.value?.height ?? Math.min(860, height - topInset.value - (height <= 640 ? 16 : 20)), minHeight.value, height),
    }
  })
  const windowStyle = computed(() => ({
    '--plugin-manager-width': `${windowSize.value.width}px`,
    '--plugin-manager-height': `${windowSize.value.height}px`,
  }))
  let drag: {
    corner: Corner; pointerId: number; handle: HTMLElement; element: HTMLElement
    x: number; y: number; rect: DOMRect; baseLeft: number; baseTop: number
    left: string; top: string; preference: Size | null
  } | null = null

  function persist() {
    try {
      if (preferred.value) localStorage.setItem(STORAGE_KEY, JSON.stringify(preferred.value))
      else localStorage.removeItem(STORAGE_KEY)
    } catch { /* Window layout must not interrupt plugin operations. */ }
  }

  function recenter() {
    const element = card.value?.$el
    if (!open.value || !element) return
    element.style.left = ''
    element.style.top = `${Math.min(topInset.value, viewport.value.height - windowSize.value.height) - topInset.value}px`
  }

  function applyDelta(current: NonNullable<typeof drag>, dx: number, dy: number) {
    const { rect, corner, element } = current
    const west = corner.endsWith('w'), north = corner.startsWith('n')
    const left = west ? clamp(rect.left + dx, 0, rect.right - minWidth.value) : rect.left
    const right = west ? rect.right : clamp(rect.right + dx, rect.left + minWidth.value, viewport.value.width)
    const top = north ? clamp(rect.top + dy, 0, rect.bottom - minHeight.value) : rect.top
    const bottom = north ? rect.bottom : clamp(rect.bottom + dy, rect.top + minHeight.value, viewport.value.height)
    const width = right - left, height = bottom - top
    if (width === windowSize.value.width && height === windowSize.value.height) return
    preferred.value = { width, height }
    // Naive UI drags with relative offsets. Compensate for the auto margins changing
    // with the width, so the opposite corner stays fixed and header dragging still works.
    element.style.left = `${left - current.baseLeft - (rect.width - width) / 2}px`
    element.style.top = `${top - current.baseTop}px`
  }

  function snapshot(corner: Corner, handle: HTMLElement, pointerId: number, x: number, y: number) {
    const element = card.value!.$el
    const rect = element.getBoundingClientRect()
    return { corner, pointerId, handle, element, x, y, rect,
      baseLeft: rect.left - (Number.parseFloat(element.style.left) || 0),
      baseTop: rect.top - (Number.parseFloat(element.style.top) || 0),
      left: element.style.left, top: element.style.top, preference: preferred.value }
  }

  function move(event: PointerEvent) {
    if (drag && event.pointerId === drag.pointerId) applyDelta(drag, event.clientX - drag.x, event.clientY - drag.y)
  }

  function finish(cancel = false) {
    const current = drag
    if (!current) return
    drag = null
    resizingWindow.value = null
    window.removeEventListener('pointermove', move)
    window.removeEventListener('pointerup', release)
    window.removeEventListener('pointercancel', cancelPointer)
    window.removeEventListener('blur', blur)
    if (cancel) {
      preferred.value = current.preference
      current.element.style.left = current.left
      current.element.style.top = current.top
    } else if (preferred.value !== current.preference) persist()
    try {
      if (current.handle.hasPointerCapture(current.pointerId)) current.handle.releasePointerCapture(current.pointerId)
    } catch { /* The manager may have closed during the gesture. */ }
  }
  function release(event: PointerEvent) { if (event.pointerId === drag?.pointerId) finish() }
  function cancelPointer(event: PointerEvent) { if (event.pointerId === drag?.pointerId) finish(true) }
  function blur() { finish() }

  function startWindowResize(corner: Corner, event: PointerEvent) {
    if (event.button !== 0 || !open.value || !card.value) return
    finish()
    event.preventDefault()
    event.stopPropagation()
    const handle = event.currentTarget as HTMLElement
    handle.focus({ preventScroll: true })
    drag = snapshot(corner, handle, event.pointerId, event.clientX, event.clientY)
    resizingWindow.value = corner
    try { handle.setPointerCapture(event.pointerId) } catch { /* Window listeners cover capture fallback. */ }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', release)
    window.addEventListener('pointercancel', cancelPointer)
    window.addEventListener('blur', blur)
  }

  function resetWindowSize() {
    finish(true)
    preferred.value = null
    persist()
    recenter()
  }

  function resizeWindowWithKeyboard(corner: Corner, event: KeyboardEvent) {
    if (event.key === 'Escape' && drag) {
      finish(true)
    } else if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key) && open.value && card.value) {
      finish()
      const step = event.shiftKey ? 48 : 16
      const dx = event.key === 'ArrowLeft' ? -step : event.key === 'ArrowRight' ? step : 0
      const dy = event.key === 'ArrowUp' ? -step : event.key === 'ArrowDown' ? step : 0
      applyDelta(snapshot(corner, event.currentTarget as HTMLElement, 0, 0, 0), dx, dy)
      persist()
    } else return
    event.preventDefault()
    event.stopPropagation()
  }

  function resizeViewport() {
    finish(true)
    viewport.value = { width: window.innerWidth, height: window.innerHeight }
    recenter()
  }
  watch([open, card], () => {
    if (!open.value) finish()
    else recenter()
  }, { flush: 'post' })
  onMounted(() => {
    try {
      const stored = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null')
      if (stored && Number.isFinite(stored.width) && stored.width > 0 && Number.isFinite(stored.height) && stored.height > 0) preferred.value = stored
    } catch { /* Invalid layout preferences use the responsive defaults. */ }
    window.addEventListener('resize', resizeViewport)
    recenter()
  })
  onScopeDispose(() => {
    finish()
    window.removeEventListener('resize', resizeViewport)
  })
  return { windowStyle, resizingWindow, startWindowResize, resizeWindowWithKeyboard, resetWindowSize }
}
