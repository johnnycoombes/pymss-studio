export type WindowCloseGuard = () => void | Promise<void>
export type WindowCloseRecovery = 'retry' | 'discard' | 'cancel'

export class WindowClosePersistenceError extends Error {
  readonly discardAndRelease: () => Promise<void>
  constructor(cause: unknown, discardAndRelease: () => Promise<void>) {
    super(cause instanceof Error ? cause.message : String(cause))
    this.name = 'WindowClosePersistenceError'
    this.discardAndRelease = discardAndRelease
  }
}

const windowCloseGuards = new Map<WindowCloseGuard, number>()

export function registerWindowCloseGuard(guard: WindowCloseGuard, priority = 0) {
  windowCloseGuards.set(guard, priority)
  return () => windowCloseGuards.delete(guard)
}

export async function runWindowCloseGuards(
  recover?: (error: WindowClosePersistenceError) => Promise<WindowCloseRecovery>,
) {
  const guards = [...windowCloseGuards.entries()]
    .sort((left, right) => right[1] - left[1])
  for (const [guard] of guards) {
    for (;;) {
      try { await guard(); break }
      catch (error) {
        if (!(error instanceof WindowClosePersistenceError) || !recover) throw error
        const action = await recover(error)
        if (action === 'retry') continue
        if (action !== 'discard') throw error
        await error.discardAndRelease()
        break
      }
    }
  }
}
