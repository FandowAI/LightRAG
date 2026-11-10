import { useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { requestId, type BatchClient, type TrainingExport, type TrainingFile, type TrainingFormat, type TrainingSelection } from '@/api/batchControl'
import Button from '@/components/ui/Button'
import useRequestScope from '@/hooks/useRequestScope'

export default function BatchTrainingExports({ client, batch, task }: { client: BatchClient; batch: string; task: string }) {
  const { t } = useTranslation()
  const owner = useMemo(() => ({ batch, task, client }), [batch, task, client])
  const [open, setOpen] = useState(false)
  const scope = useMemo(() => ({ owner, open }), [owner, open])
  const begin = useRequestScope(scope)
  const [format, setFormat] = useState<TrainingFormat>('messages')
  const [selection, setSelection] = useState<TrainingSelection>('processed')
  const [refresh, setRefresh] = useState(0)
  const [loaded, setLoaded] = useState<{ scope: typeof scope; items: TrainingExport[]; error?: string } | null>(null)
  const [creating, setCreating] = useState<typeof scope | null>(null)
  const [error, setError] = useState<{ scope: typeof scope; message: string } | null>(null)
  const attempt = useRef<{ owner: typeof owner; format: TrainingFormat; selection: TrainingSelection; request: string } | null>(null)
  const current = loaded?.scope === scope ? loaded : null
  const busy = creating === scope || current?.items.some((item) => ['queued', 'running'].includes(item.state))
  useEffect(() => {
    if (!open) return
    const request = begin()
    let timer: ReturnType<typeof setTimeout> | undefined
    async function poll() {
      try {
        const result = await client.trainingExports(batch, task, request.signal)
        if (request.current()) {
          setLoaded({ scope, items: result.items })
          if (result.items.some((item) => ['queued', 'running'].includes(item.state))) timer = setTimeout(() => void poll(), 2000)
        }
      } catch (failure) {
        if (request.current()) setLoaded((value) => ({ scope, items: value?.scope === scope ? value.items : [], error: String(failure) }))
      }
    }
    void poll()
    return () => { request.cancel(); if (timer) clearTimeout(timer) }
  }, [open, batch, task, client, scope, begin, refresh])
  async function create() {
    const request = begin()
    if (!request.current()) { request.finish(); return }
    let pending = attempt.current
    if (!pending || pending.owner !== owner || pending.format !== format || pending.selection !== selection) {
      pending = { owner, format, selection, request: requestId() }
      attempt.current = pending
    }
    setCreating(scope)
    setError(null)
    try {
      const result = await client.createTrainingExport(batch, task, format, selection, pending.request, request.signal)
      if (attempt.current === pending) attempt.current = null
      if (request.current()) {
        setLoaded((value) => ({ scope, items: [...(value?.scope === scope ? value.items.filter((item) => item.export_id !== result.export_id) : []), result] }))
        setRefresh((value) => value + 1)
      }
    } catch (failure) {
      if (request.current()) setError({ scope, message: String(failure) })
    } finally { if (request.current()) setCreating(null); request.finish() }
  }
  async function download(item: TrainingExport, artifact: TrainingFile) {
    const request = begin()
    if (!request.current()) { request.finish(); return }
    setError(null)
    try {
      const blob = await client.trainingExportDownload(batch, task, item.export_id, artifact, request.signal)
      if (!request.current()) return
      const url = URL.createObjectURL(blob)
      try {
        const link = document.createElement('a')
        link.href = url
        link.download = `${artifact}-${item.format}-${item.export_id}.${artifact === 'manifest' ? 'json' : artifact === 'log' ? 'log' : 'jsonl'}`
        document.body.appendChild(link)
        link.click()
        link.remove()
      } finally { URL.revokeObjectURL(url) }
    } catch (failure) {
      if (request.current()) setError({ scope, message: String(failure) })
    } finally { request.finish() }
  }
  return <section aria-label={t('batchWorkbench.trainingExport')} className="space-y-2">
    <Button size="sm" variant="outline" onClick={() => setOpen((value) => !value)}>{t('batchWorkbench.trainingExport')}</Button>
    {open && <div className="space-y-3 rounded border p-3">
      <p className="text-sm text-muted-foreground">{t('batchWorkbench.trainingHelp')}</p>
      <div className="flex flex-wrap items-end gap-3">
        <label className="space-y-1 text-sm">{t('batchWorkbench.trainingFormat')}
          <select aria-label={t('batchWorkbench.trainingFormat')} className="block rounded border bg-background p-2" disabled={busy} value={format} onChange={(event) => setFormat(event.target.value as TrainingFormat)}>
            <option value="messages">Messages</option><option value="alpaca">Alpaca</option><option value="sharegpt">ShareGPT</option>
          </select>
        </label>
        <label className="space-y-1 text-sm">{t('batchWorkbench.trainingSelection')}
          <select aria-label={t('batchWorkbench.trainingSelection')} className="block rounded border bg-background p-2" disabled={busy} value={selection} onChange={(event) => setSelection(event.target.value as TrainingSelection)}>
            <option value="processed">{t('batchWorkbench.trainingProcessed')}</option><option value="machine_passed">{t('batchWorkbench.trainingPassed')}</option>
          </select>
        </label>
        <Button size="sm" disabled={busy} onClick={() => void create()}>{t('batchWorkbench.trainingCreate')}</Button>
        <Button size="sm" variant="outline" onClick={() => setRefresh((value) => value + 1)}>{t('batchWorkbench.trainingRefresh')}</Button>
      </div>
      {current?.error && <p role="alert" className="text-destructive">{current.error}</p>}
      {error?.scope === scope && <p role="alert" className="text-destructive">{error.message}</p>}
      {current?.items.map((item) => <article key={item.export_id} className="space-y-2 border-t pt-2 text-sm">
        <p>{item.format} · {t(`batchWorkbench.states.${item.state}`, item.state)} · {t('batchWorkbench.trainingCount', { count: item.counts.samples ?? 0 })}</p>
        {item.error && <p className="text-destructive">{item.error}</p>}
        {item.state === 'completed' && <div className="flex flex-wrap gap-2">{(['dataset', 'provenance', 'manifest'] as TrainingFile[]).map((artifact) => <Button key={artifact} size="sm" variant="outline" onClick={() => void download(item, artifact)}>{t(`batchWorkbench.trainingFiles.${artifact}`)}</Button>)}</div>}
        {['failed', 'interrupted'].includes(item.state) && <Button size="sm" variant="outline" onClick={() => void download(item, 'log')}>{t('batchWorkbench.trainingFiles.log')}</Button>}
      </article>)}
    </div>}
  </section>
}
