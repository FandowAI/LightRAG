import { useTranslation } from 'react-i18next'
import Button from '@/components/ui/Button'
import { batchIsActive, useBatchRetrieval } from './useBatchRetrieval'

export default function RetrievalProgress({ batch }: { batch: ReturnType<typeof useBatchRetrieval> }) {
  const { t } = useTranslation()
  const job = batch.job
  if (!job) return <p className="text-sm text-muted-foreground">{t('batchFlow.noRetrieval')}</p>
  const active = batchIsActive(job)
  return <section className="space-y-3" aria-label={t('batch.progress_label')}>
    <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="font-medium">{t('batchFlow.retrievalProgress')}</h3>{active && <Button type="button" size="sm" variant="ghost" disabled={batch.busy || job.cancel_requested} onClick={() => void batch.stop()}>{t(job.cancel_requested ? 'batch.stopping' : 'batch.stop')}</Button>}</div>
    <div role="status" className="flex flex-wrap items-baseline justify-between gap-2 text-sm"><span>{t('batch.progress', { processed: job.processed, total: job.total })}</span><span className="font-semibold">{t('batch.package_count', { count: job.package_count })}</span></div>
    <progress aria-label={t('batch.progress_label')} value={active && job.phase ? job.phase_done || 0 : job.processed} max={active && job.phase ? Math.max(job.phase_total || 0, 1) : Math.max(job.total, 1)} className="h-2 w-full accent-emerald-500" />
    <p className="text-sm text-muted-foreground">{active ? job.cancel_requested ? t('batch.wait_stop') : job.phase ? t(`batch.phase.${job.phase}`, { done: job.phase_done || 0, total: job.phase_total || 0 }) : t('batch.current', { name: job.current_center || '…', percent: Math.floor(job.processed / Math.max(job.total, 1) * 100) }) : t(`batch.status.${job.status}`)}</p>
    {!!job.duplicate_chunks && <p className="text-sm text-muted-foreground">{t('batch.deduplicated', { count: job.duplicate_chunks, skipped: job.skipped || 0 })}</p>}
    {(job.failed > 0 || job.empty > 0) && <details className="text-sm text-muted-foreground"><summary className="cursor-pointer">{t('batch.exceptions', { failed: job.failed, empty: job.empty })}</summary><ul className="mt-2 space-y-1">{job.items.filter(item => ['failed', 'empty'].includes(item.status)).map(item => <li key={item.center}>{item.center} · {t(`batch.item.${item.status}`)}</li>)}</ul></details>}
    {job.export_path && <details className="text-xs text-muted-foreground"><summary>{t('batchFlow.taskDetails')}</summary><p className="mt-2 break-all">{t('batch.saved_path')}<code>{job.export_path}</code></p></details>}
  </section>
}
