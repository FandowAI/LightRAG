import { useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import Button from '@/components/ui/Button'
import { requestId, type BatchClient, type BatchTask } from '@/api/batchControl'
import BatchTaskArtifacts from './BatchTaskArtifacts'
import useRequestScope from '@/hooks/useRequestScope'

export default function BatchTaskProgress({ client, batch, refresh, onTasks }: {
  client: BatchClient; batch: string; refresh: number; onTasks: (tasks: BatchTask[]) => void
}) {
  const { t } = useTranslation()
  const context = useMemo(() => ({ batch, client }), [batch, client])
  const begin = useRequestScope(context)
  const [loaded, setLoaded] = useState<{ context: typeof context; tasks: BatchTask[] } | null>(null)
  const tasks = loaded?.context === context ? loaded.tasks : []
  const [error, setError] = useState<{ context: typeof context; message: string } | null>(null)
  const [acting, setActing] = useState<typeof context | null>(null)
  const attempts = useRef(new WeakMap<object, Map<string, { revision: string; request: string }>>())
  useEffect(() => {
    const request = begin()
    let timer: ReturnType<typeof setTimeout> | undefined
    const poll = async () => {
      try {
        const result = await client.tasks(batch, request.signal)
        if (request.current()) { setLoaded({ context, tasks: result.items }); onTasks(result.items); setError(null) }
      } catch (failure) {
        if (request.current()) setError({ context, message: String(failure) })
      } finally {
        if (request.current()) timer = setTimeout(() => void poll(), 2000)
      }
    }
    void poll()
    return () => { request.cancel(); if (timer) clearTimeout(timer) }
  }, [batch, client, refresh, onTasks, context, begin])
  async function action(task: BatchTask, retry: boolean) {
    const request = begin()
    if (!request.current()) { request.finish(); return }
    const pending = attempts.current.get(context) ?? new Map<string, { revision: string; request: string }>()
    attempts.current.set(context, pending)
    setActing(context)
    try {
      if (retry) {
        let attempt = pending.get(task.task_id)
        if (!attempt) {
          const settings = await client.settings(batch, request.signal)
          if (!request.current()) return
          attempt = { revision: settings.batch.active, request: requestId() }
          pending.set(task.task_id, attempt)
        }
        await client.retryTask(batch, task.task_id, attempt.revision, attempt.request)
        pending.delete(task.task_id)
      } else await client.cancelTask(batch, task.task_id)
      if (!request.current()) return
      const result = await client.tasks(batch)
      if (request.current()) { setLoaded({ context, tasks: result.items }); onTasks(result.items); setError(null) }
    } catch (failure) {
      if ((failure as { status?: number }).status === 409) pending.delete(task.task_id)
      if (request.current()) setError({ context, message: String(failure) })
    } finally { if (request.current()) setActing(null); request.finish() }
  }
  return <section aria-label={t('batchWorkbench.progress')} className="space-y-3 rounded-lg border p-4">
    <h2 className="font-semibold">{t('batchWorkbench.progress')}</h2>
    {error?.context === context && <p role="alert" className="text-destructive">{error.message}</p>}
    {!tasks.length && <p className="text-muted-foreground">{t('batchWorkbench.noTasks')}</p>}
    {[...tasks].reverse().map((task) => <article key={task.task_id} className="space-y-2 border-t pt-3">
      <div className="flex flex-wrap items-center gap-3">
        <strong>{t(`batchWorkbench.kinds.${task.kind}`)}</strong>
        <span>{t(`batchWorkbench.states.${task.state}`, task.state)}</span>
        <span className="text-muted-foreground">{task.task_id}</span>
        {!task.terminal && <Button size="sm" variant="outline" disabled={acting === context || task.cancel_requested} onClick={() => void action(task, false)}>{t(task.cancel_requested ? 'batchWorkbench.cancelling' : 'batchWorkbench.cancel')}</Button>}
        {['failed', 'cancelled', 'interrupted', 'completed_with_errors'].includes(task.state) && <Button size="sm" variant="outline" disabled={acting === context} onClick={() => void action(task, true)}>{t('batchWorkbench.retry')}</Button>}
      </div>
      {task.progress && <div className="flex flex-wrap gap-3 text-sm">{['processed', 'total', 'failed', 'packages', 'accepted_sources', 'rejected_sources'].map((key) => task.progress?.[key] != null ? <span key={key}>{t(`batchWorkbench.counts.${key}`)}: {task.progress[key]}</span> : null)}</div>}
      {task.waiting_reason && <p role="status" className="text-sm">{task.waiting_reason}</p>}
      {task.provider_admission && (task.provider_admission.queued > 0 || task.provider_admission.active > 0) && <p role="status" aria-label={t('batchWorkbench.modelAdmission')} className="text-sm">
        {t('batchWorkbench.providerQueued', { count: task.provider_admission.queued })}
        {' · '}{t('batchWorkbench.providerActive', { count: task.provider_admission.active })}
      </p>}
      {task.error && <p className="text-sm text-destructive">{task.error}</p>}
      <BatchTaskArtifacts client={client} batch={batch} task={task} />
    </article>)}
  </section>
}
