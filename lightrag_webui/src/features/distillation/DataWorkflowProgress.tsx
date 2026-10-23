import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import Button from '@/components/ui/Button'
import { getWorkflowLogs, type WorkflowJob } from '@/api/dataWorkflow'

export default function DataWorkflowProgress({ job }: { job: WorkflowJob }) {
  const { t } = useTranslation()
  const [logs, setLogs] = useState('')
  const [offset, setOffset] = useState(0)
  const [loading, setLoading] = useState(false)
  const [logError, setLogError] = useState(false)
  const request = useRef<AbortController | null>(null)
  useEffect(() => () => request.current?.abort(), [])
  const loadLogs = async () => {
    if (loading) return
    const controller = new AbortController()
    request.current = controller
    setLoading(true); setLogError(false)
    try {
      const result = await getWorkflowLogs(job.job_id, offset, controller.signal)
      if (!controller.signal.aborted) {
        setLogs(previous => (previous + result.text).slice(-65536)); setOffset(result.next_offset)
      }
    } catch { if (!controller.signal.aborted) setLogError(true) }
    finally { if (!controller.signal.aborted) setLoading(false) }
  }
  const progress = job.progress
  const percent = progress.percent
  return <section className="space-y-3 rounded-md border p-4" aria-label={t('distillation.progress_title')}>
    <h3 className="font-medium">{t('distillation.progress_title')}</h3>
    <p role="status">{t(`distillation.status.${job.status}`)}</p>
    {!job.terminal && <p className="text-sm text-muted-foreground">{t(`distillation.phase.${job.phase}`, { defaultValue: t('distillation.processing') })}</p>}
    {(!job.terminal || percent !== null) && <progress aria-label={t('distillation.progress_title')} value={percent ?? undefined} max={100} className="h-2 w-full accent-emerald-500" />}
    {percent !== null && <p className="text-sm tabular-nums">{percent}%</p>}
    <p className="text-sm text-muted-foreground">{t('distillation.counts', { seen: progress.seen_sources, accepted: progress.accepted_sources, rejected: progress.rejected_sources, processed: progress.processed_sources ?? t('distillation.unknown') })}</p>
    {job.error && <p role="alert" className="whitespace-pre-wrap break-words text-sm text-destructive">{job.error}</p>}
    <details>
      <summary className="cursor-pointer text-sm">{t('distillation.details')}</summary>
      <p className="my-2 break-all text-xs">{t('distillation.job_id')}: {job.job_id}</p>
      <Button type="button" variant="outline" disabled={loading} onClick={() => void loadLogs()}>{t('distillation.load_logs')}</Button>
      {logError && <p role="alert">{t('distillation.log_error')}</p>}
      <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words text-xs" aria-label={t('distillation.logs')}>{logs || t('distillation.no_logs')}</pre>
    </details>
  </section>
}
