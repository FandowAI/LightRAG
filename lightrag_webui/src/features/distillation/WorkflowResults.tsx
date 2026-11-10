import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import Button from '@/components/ui/Button'
import { getWorkflowResults, downloadWorkflowArtifact, type WorkflowJob, type WorkflowResultsPage } from '@/api/dataWorkflow'
import { useBatchContext } from '@/stores/batchContext'
import useRequestScope from '@/hooks/useRequestScope'

export default function WorkflowResults({ job }: { job: WorkflowJob }) {
  const { t } = useTranslation()
  const epoch = useBatchContext((state) => state.epoch)
  const scope = useMemo(() => ({ id: job.job_id, epoch }), [job.job_id, epoch])
  const begin = useRequestScope(scope)
  const [open, setOpen] = useState(false)
  const [offset, setOffset] = useState(0)
  const [page, setPage] = useState<WorkflowResultsPage | null>(null)
  const [error, setError] = useState('')
  const [downloadError, setDownloadError] = useState('')
  const [downloading, setDownloading] = useState(false)
  const available = ['completed', 'completed_with_errors'].includes(job.status) && !!job.result
  useEffect(() => {
    if (!open || !available) return
    const request = begin()
    void getWorkflowResults(job.job_id, offset, request.signal).then((value) => {
      if (request.current()) { setPage(value); setError('') }
    }).catch((failure) => { if (request.current()) setError(String(failure)) })
    return request.cancel
  }, [open, available, job.job_id, offset, begin])
  async function download(artifact: 'review' | 'stderr') {
    const request = begin()
    setDownloading(true); setDownloadError('')
    try {
      const blob = await downloadWorkflowArtifact(job.job_id, artifact, request.signal)
      if (!request.current()) return
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url; link.download = `${job.job_id}-${artifact === 'review' ? 'review.jsonl' : 'stderr.log'}`
      document.body.appendChild(link); link.click(); link.remove()
      setTimeout(() => URL.revokeObjectURL(url), 0)
    } catch (failure) { if (request.current()) setDownloadError(String(failure)) }
    finally { if (request.current()) setDownloading(false); request.finish() }
  }
  return <section className="space-y-3 border-t pt-4" aria-label={t('batchFlow.results')}>
    <h4 className="font-medium">{t('batchFlow.results')}</h4>
    <p className="text-xs text-muted-foreground">{t(available ? 'batchFlow.fullResultHint' : 'batchFlow.resultsPending')}</p>
    <div className="flex flex-wrap gap-2">
      <Button size="sm" variant="outline" disabled={!available} onClick={() => setOpen((value) => !value)}>{t('batchFlow.viewQa')}</Button>
      <Button size="sm" variant="outline" disabled={!available || downloading} onClick={() => void download('review')}>{t('batchFlow.downloadResult')}</Button>
      {job.terminal && <Button size="sm" variant="ghost" disabled={downloading} onClick={() => void download('stderr')}>{t('batchFlow.downloadLogs')}</Button>}
    </div>
    {downloadError && <p role="alert" className="text-sm text-destructive">{downloadError}</p>}
    {open && <div className="space-y-3">
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      {!page && !error && <p role="status">{t('batchFlow.loadingResults')}</p>}
      {page?.items.map((item) => <article key={`${item.source_id}:${item.work_item_id}:${item.version_id}`} className="space-y-2 rounded-md bg-muted/50 p-3 text-sm">
        <p className="whitespace-pre-wrap break-words font-medium">{item.question}</p>
        <p className="whitespace-pre-wrap break-words">{item.answer}</p>
      </article>)}
      {page && !page.items.length && <p className="text-sm text-muted-foreground">{t('batchFlow.emptyResults')}</p>}
      {page && <div className="flex gap-2">
        <Button size="sm" variant="outline" disabled={offset === 0} onClick={() => { setPage(null); setOffset(Math.max(0, offset - 20)) }}>{t('batchFlow.previous')}</Button>
        <Button size="sm" variant="outline" disabled={!page.has_more} onClick={() => { setPage(null); setOffset(page.next_offset) }}>{t('batchFlow.next')}</Button>
      </div>}
    </div>}
  </section>
}
