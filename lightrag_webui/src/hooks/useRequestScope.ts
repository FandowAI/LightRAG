import { useCallback, useLayoutEffect, useRef } from 'react'

/** Cancel pending continuations on scope changes, including StrictMode remounts. */
export default function useRequestScope(scope: object) {
  const active = useRef<object | null>(null)
  const pending = useRef(new Set<AbortController>())
  useLayoutEffect(() => {
    active.current = scope
    const controllers = pending.current
    return () => {
      if (active.current === scope) active.current = null
      for (const controller of controllers) controller.abort()
      controllers.clear()
    }
  }, [scope])
  return useCallback(() => {
    const controller = new AbortController()
    pending.current.add(controller)
    if (active.current !== scope) controller.abort()
    const finish = () => { pending.current.delete(controller) }
    return {
      signal: controller.signal,
      current: () => active.current === scope && !controller.signal.aborted,
      finish,
      cancel: () => { controller.abort(); finish() }
    }
  }, [scope])
}
