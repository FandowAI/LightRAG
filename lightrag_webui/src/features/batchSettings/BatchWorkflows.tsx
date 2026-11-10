import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import Button from '@/components/ui/Button'
import { requestId, type BatchClient, type BatchSettings, type BatchTask, type BatchTaskCommand } from '@/api/batchControl'
import BatchTaskProgress from './BatchTaskProgress'

export default function BatchWorkflows({ client, batch }: { client: BatchClient; batch: string }) {
  const { t } = useTranslation()
  const [settings, setSettings] = useState<BatchSettings | null>(null)
  const [kind, setKind] = useState<BatchTaskCommand['kind']>('centers')
  const [source, setSource] = useState('')
  const [retrieval, setRetrieval] = useState('')
  const [centers, setCenters] = useState('')
  const [tasks, setTasks] = useState<BatchTask[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [refresh, setRefresh] = useState(0)
  const attempt = useRef<{ body: string; id: string } | null>(null)
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  useEffect(() => {
    const controller = new AbortController()
    void client.settings(batch, controller.signal).then((value) => {
      if (!controller.signal.aborted) { setSettings(value); setSource(value.configuration.bridge.input_ids[0] ?? ''); setError('') }
    }).catch((failure) => { if (!controller.signal.aborted) setError(String(failure)) })
    return () => controller.abort()
  }, [batch, client])
  const onTasks = useCallback((items: BatchTask[]) => setTasks(items), [])
  const latestCenters = [...tasks].reverse().find((task) => task.state === 'completed' && task.result?.centers)?.result?.centers?.candidates ?? []
  const evidence = tasks.filter((task) => task.terminal && ['completed', 'completed_with_errors'].includes(task.state) && task.result?.retrieval?.job_id)
  const directory = kind === 'ingest' || kind === 'chain' || (kind === 'distillation' && !retrieval)
  const needsCenters = kind === 'retrieval' || kind === 'chain'
  async function submit() {
    if (!settings || busy) return
    setBusy(true)
    setError('')
    try {
      const current = await client.settings(batch)
      const command: BatchTaskCommand = {
        expected_revision: current.batch.active, request_id: '', kind,
        source: directory ? { data_batch_id: batch, type: 'directory', input_ids: [source] }
          : kind === 'distillation' ? { data_batch_id: batch, type: 'retrieval', retrieval_job_id: retrieval }
            : { data_batch_id: batch, type: 'index' },
        ...(needsCenters ? { centers: centers.split(/\r?\n/).map((name) => name.trim()).filter(Boolean) } : {})
      }
      const body = JSON.stringify(command)
      if (attempt.current?.body !== body) attempt.current = { body, id: requestId() }
      command.request_id = attempt.current.id
      await client.submit(batch, command)
      attempt.current = null
      if (mounted.current) { setSettings(current); setRefresh((value) => value + 1) }
    } catch (failure) { if (mounted.current) setError(String(failure)) }
    finally { if (mounted.current) setBusy(false) }
  }
  return <div className="space-y-4 p-4">
    <section className="space-y-4 rounded-lg border p-4">
      <h1 className="text-lg font-semibold">{t('batchWorkbench.workflow')}</h1>
      {error && <p role="alert" className="text-destructive">{error}</p>}
      <label className="flex flex-wrap items-center gap-2">{t('batchWorkbench.operation')}
        <select aria-label={t('batchWorkbench.operation')} className="bg-background rounded border p-2" value={kind} disabled={busy} onChange={(event) => setKind(event.target.value as BatchTaskCommand['kind'])}>
          {(['ingest', 'centers', 'retrieval', 'distillation', 'chain'] as const).map((value) => <option key={value} value={value}>{t(`batchWorkbench.kinds.${value}`)}</option>)}
        </select>
      </label>
      {kind === 'distillation' && <label className="block space-y-1">{t('batchWorkbench.evidence')}
        <select aria-label={t('batchWorkbench.evidence')} className="bg-background rounded border p-2" value={retrieval} onChange={(event) => setRetrieval(event.target.value)}>
          <option value="">{t('batchWorkbench.fromDirectory')}</option>
          {evidence.map((task) => <option key={task.task_id} value={task.result!.retrieval!.job_id}>{task.result!.retrieval!.job_id}</option>)}
        </select>
      </label>}
      {directory && <label className="block space-y-1">{t('batchWorkbench.source')}
        <select aria-label={t('batchWorkbench.source')} className="bg-background rounded border p-2" value={source} onChange={(event) => setSource(event.target.value)}>
          {settings?.configuration.bridge.input_ids.map((id) => <option key={id} value={id}>{id}</option>)}
        </select>
      </label>}
      {needsCenters && <label className="block space-y-2">{t('batchWorkbench.centers')}
        <textarea aria-label={t('batchWorkbench.centers')} className="bg-background min-h-40 w-full rounded border p-2" value={centers} onChange={(event) => setCenters(event.target.value)} />
        {!!latestCenters.length && <Button type="button" variant="outline" onClick={() => setCenters(latestCenters.map((row) => row.name).join('\n'))}>{t('batchWorkbench.useCenters', { count: latestCenters.length })}</Button>}
      </label>}
      <Button disabled={busy || !settings || !!settings.batch.pending || (directory && !source) || (needsCenters && !centers.trim())} onClick={() => void submit()}>{t(busy ? 'batchWorkbench.submitting' : 'batchWorkbench.start')}</Button>
      {settings?.batch.pending && <p>{t('batchWorkbench.pending')}</p>}
    </section>
    <BatchTaskProgress client={client} batch={batch} refresh={refresh} onTasks={onTasks} />
  </div>
}
