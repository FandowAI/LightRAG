import { lazy, Suspense, useLayoutEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import Button from '@/components/ui/Button'
import type { WorkflowJob } from '@/api/dataWorkflow'
import useWorkflowLogs from './useWorkflowLogs'
const WorkflowResults = lazy(() => import('./WorkflowResults'))

export default function DataWorkflowProgress({ job }: { job: WorkflowJob }) {
  const { t } = useTranslation()
  const logs = useWorkflowLogs(job)
  const [tab, setTab] = useState<'summary' | 'run' | 'error'>('run')
  const [following, setFollowing] = useState(true)
  const [seen, setSeen] = useState(0)
  const viewport = useRef<HTMLPreElement>(null)
  useLayoutEffect(() => {
    if (following && viewport.current) viewport.current.scrollTop = viewport.current.scrollHeight
  }, [logs.text, following, tab])
  const progress = job.progress
  const percent = progress.percent
  const errorLines = logs.text.split('\n').filter((line) => /error|exception|traceback|failed|错误|失败/i.test(line)).join('\n')
  return <section className="space-y-3" aria-label={t('distillation.progress_title')}>
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h3 className="font-medium">{t('distillation.progress_title')}</h3>
      <span className="text-xs text-muted-foreground">{t(logs.reconnecting ? 'batchFlow.reconnecting' : job.terminal ? 'batchFlow.finished' : 'batchFlow.live')}</span>
    </div>
    <p role="status">{t(`distillation.status.${job.status}`)}</p>
    {!job.terminal && <p className="text-sm text-muted-foreground">{t(`distillation.phase.${job.phase}`, { defaultValue: t('distillation.processing') })}</p>}
    {(!job.terminal || percent !== null) && <progress aria-label={t('distillation.progress_title')} value={percent ?? undefined} max={100} className="h-2 w-full accent-emerald-500" />}
    {percent !== null && <p className="text-sm tabular-nums">{percent}%</p>}
    <p className="text-sm text-muted-foreground">{t('distillation.counts', { seen: progress.seen_sources, accepted: progress.accepted_sources, rejected: progress.rejected_sources, processed: progress.processed_sources ?? t('distillation.unknown') })}</p>
    {job.error && <p role="alert" className="whitespace-pre-wrap break-words text-sm text-destructive">{job.error}</p>}
    <div className="flex flex-wrap gap-2 border-b pb-2" role="group" aria-label={t('batchFlow.logTabs')}>
      {(['summary', 'run', 'error'] as const).map((value) => <Button type="button" size="sm" variant={tab === value ? 'secondary' : 'ghost'} aria-pressed={tab === value} key={value} onClick={() => setTab(value)}>{t(`batchFlow.logs.${value}`)}</Button>)}
    </div>
    {tab === 'summary' ? <dl className="space-y-2 text-sm">
      <div className="flex justify-between gap-2"><dt>{t('batchFlow.sourceTotal')}</dt><dd>{progress.accepted_sources}</dd></div>
      <div className="flex justify-between gap-2"><dt>{t('batchFlow.sourceProcessed')}</dt><dd>{progress.processed_sources ?? t('distillation.unknown')}</dd></div>
      <div className="flex justify-between gap-2"><dt>{t('batchFlow.currentPhase')}</dt><dd>{t(`distillation.status.${job.status}`)}</dd></div>
    </dl> : <>
      {tab === 'error' && <p className="text-xs text-muted-foreground">{t('batchFlow.errorFilterHint')}</p>}
      <pre ref={viewport} onScroll={(event) => {
        const node = event.currentTarget
        const bottom = node.scrollHeight - node.scrollTop - node.clientHeight < 24
        setFollowing(bottom)
        if (bottom || following) setSeen(logs.version)
      }} className="max-h-72 min-h-40 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted/50 p-3 text-xs" aria-label={t('distillation.logs')}>{tab === 'error' ? errorLines || t('batchFlow.noErrors') : logs.text || t('distillation.no_logs')}</pre>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Button type="button" size="sm" variant="ghost" aria-pressed={following} onClick={() => { setFollowing((value) => !value); setSeen(logs.version) }}>{t(following ? 'batchFlow.following' : logs.version > seen ? 'batchFlow.newLogs' : 'batchFlow.follow')}</Button>
        <span className="text-xs text-muted-foreground">{logs.updated ? t('batchFlow.updated', { time: new Date(logs.updated).toLocaleTimeString() }) : t('batchFlow.connecting')}</span>
      </div>
    </>}
    {logs.reconnecting && <p role="status" className="text-xs text-muted-foreground">{t('batchFlow.logRetry')}</p>}
    {logs.trimmed && <p className="text-xs text-muted-foreground">{t('batchFlow.logWindow')}</p>}
    <details className="text-xs text-muted-foreground"><summary>{t('batchFlow.taskDetails')}</summary><p className="mt-2 break-all">{job.job_id}</p></details>
    <Suspense fallback={<p role="status">{t('batchFlow.loadingResults')}</p>}><WorkflowResults key={job.job_id} job={job} /></Suspense>
  </section>
}
