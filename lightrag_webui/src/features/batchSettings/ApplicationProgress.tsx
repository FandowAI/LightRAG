import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import Button from '@/components/ui/Button'
import { requestId, type Application, type BatchClient } from '@/api/batchControl'

export default function ApplicationProgress({
  client,
  batch,
  initial,
  canApply,
  onFinished
}: {
  client: BatchClient
  batch: string
  initial: Application
  canApply: boolean
  onFinished: () => void
}) {
  const { t } = useTranslation()
  const [operation, setOperation] = useState(initial)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const acting = useRef(false)
  const mounted = useRef(true)
  const attempt = useRef(requestId())
  const notified = useRef('')
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])
  useEffect(() => {
    if (operation.state === 'awaiting_action' || operation.terminal) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const next = await client.application(batch, operation.operation_id, controller.signal)
        if (controller.signal.aborted) return
        setOperation(next)
        setError('')
        if (next.terminal) {
          attempt.current = requestId()
          if (notified.current !== next.state) {
            notified.current = next.state
            onFinished()
          }
          return
        }
      } catch (failure) {
        if (!controller.signal.aborted) setError(String(failure))
      }
      if (!controller.signal.aborted) timer = setTimeout(poll, 1500)
    }
    timer = setTimeout(poll, 300)
    return () => {
      controller.abort()
      clearTimeout(timer)
    }
  }, [client, batch, operation.operation_id, operation.state, operation.terminal, onFinished])

  async function act(discard: boolean) {
    if (acting.current) return
    acting.current = true
    setBusy(true)
    setError('')
    try {
      const next = discard
        ? await client.discard(batch, operation.operation_id)
        : await client.apply(
          batch,
          operation.operation_id,
          operation.config_revision_id,
          attempt.current
        )
      if (!mounted.current) return
      setOperation(next)
      if (discard) onFinished()
      else if (next.terminal) {
        attempt.current = requestId()
        onFinished()
      }
    } catch (failure) {
      if (mounted.current) setError(String(failure))
    } finally {
      acting.current = false
      if (mounted.current) setBusy(false)
    }
  }
  const unsuccessful = operation.state === 'failed' || operation.state === 'rollback_failed'
  const actionable = operation.state === 'awaiting_action' || unsuccessful
  return (
    <section
      aria-label={t('batchSettings.applicationProgress')}
      className="space-y-3 rounded-lg border border-amber-500/40 bg-amber-500/5 p-4"
    >
      <h2 className="font-medium">{t('batchSettings.applicationProgress')}</h2>
      <p role="status">{t(`batchSettings.states.${operation.state}`)}</p>
      <p className="text-muted-foreground text-sm">{t('batchSettings.applicationHint')}</p>
      {operation.waiting_reason && <p className="text-sm">{operation.waiting_reason}</p>}
      {operation.error && <p className="text-destructive text-sm">{operation.error}</p>}
      {operation.rollback_error && <p className="text-destructive text-sm">{operation.rollback_error}</p>}
      {error && (
        <p role="alert" className="text-destructive text-sm">
          {error}
        </p>
      )}
      {actionable && (
        <div className="flex flex-wrap gap-2">
          <Button type="button" disabled={!canApply || busy} onClick={() => void act(false)}>
            {t(unsuccessful ? 'batchSettings.retryApply' : 'batchSettings.apply')}
          </Button>
          <Button
            type="button"
            variant="outline"
            disabled={!canApply || busy}
            onClick={() => void act(true)}
          >
            {t('batchSettings.discardPending')}
          </Button>
        </div>
      )}
    </section>
  )
}
