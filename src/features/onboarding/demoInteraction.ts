export function lockTutorialInteraction(target: () => HTMLElement | null, parameterStep: () => boolean) {
  let dragging = false
  function restrict(event: Event) {
    if (!parameterStep() && event instanceof KeyboardEvent && event.key === 'Escape'
      && !document.querySelector('.n-base-select-menu, .n-dropdown-menu')) {
      const close = document.querySelector<HTMLElement>('.n-drawer-header__close')
      if (close) { close.click(); event.preventDefault(); event.stopImmediatePropagation(); return }
    }
    const dismissing = !parameterStep() && document.querySelector('.n-drawer, .n-modal, .n-dialog')
      && ((event instanceof KeyboardEvent && event.key === 'Escape')
        || (event.target instanceof Element && event.target.matches('.n-drawer-mask, .n-modal-mask')))
    if (dismissing) return
    if (event.type === 'drop' || event.type === 'dragover'
      || (event instanceof KeyboardEvent && (event.ctrlKey || event.metaKey) && ['s', 'o', 'p'].includes(event.key.toLowerCase()))) {
      event.preventDefault(); event.stopImmediatePropagation(); return
    }
    if (event.type === 'pointerup' || event.type === 'pointercancel') { dragging = false; return }
    if (event.type === 'pointermove' && dragging) return
    const element = event.target instanceof Element ? event.target : null
    const active = target()
    const coach = element?.closest('.beginner-guide-coach, .parameter-field-guide, .beginner-guide-paused, .beginner-guide-preparing-skip')
    const popup = element?.closest('.n-base-select-menu, .n-dropdown-menu, .n-popover, .n-dialog, .n-drawer, .n-modal')
    if (coach || (active && element && active.contains(element)) || (popup && !parameterStep())) {
      if (event.type === 'pointerdown') dragging = true
      return
    }
    if (event.cancelable) event.preventDefault()
    event.stopImmediatePropagation()
    if (event.type === 'focusin') document.querySelector<HTMLElement>('.guide-coach__actions button:last-child')?.focus({ preventScroll: true })
  }
  const events = ['click', 'dblclick', 'contextmenu', 'pointerdown', 'pointermove', 'pointerup', 'pointercancel', 'keydown', 'focusin', 'drop', 'dragover']
  events.forEach(type => window.addEventListener(type, restrict, { capture: true, passive: false }))
  return () => events.forEach(type => window.removeEventListener(type, restrict, true))
}
